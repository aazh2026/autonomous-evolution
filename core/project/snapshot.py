"""项目快照的可复现性 —— **采集到的数要落下来，而不是回头再查**。

设计依据：docs/tasks.md TASK-P1-023（§3.6 Snapshot = version + commit +
metrics、R-068、R-069、I-20）。

**快照不是"当前状态的引用"，是"当时状态的记录"。** 差别全在事后：引用会
跟着代码往前跑 —— 今天查快照，看到的是今天的覆盖率；记下来的才回答"那会儿
是多少"。所以这里每个数都在**采集时**落到快照里，**读快照不许回头再跑一遍
测试** —— 跑一遍得到的是另一个时点的数，还会顺手把工作区改脏。

**"当时是多少"要能被重建。** 给定一份快照记录加产物 URI，事后就能取回那个
时点的指标数值 —— 不依赖任何"再查一次"。这就是
:meth:`ProjectSnapshotRecord.to_document` / :meth:`from_document` 这一对往返
存在的原因，也是"落盘"这件事的落点（**落的是数，不是数的来源**）。

**同一 commit 连续构建两次，元数据必须一致。** 否则"快照"这个词就没有意义
—— 每次查出来的不一样，说明它记的不是这个 commit 的状态，而是"查的那一刻的
状态"。所以**采集时点不算元数据**：``collected_at`` 记的是"什么时候查的"，
而元数据记的是"这个 commit 当时是什么样"。前者每次查都不同，后者必须每次
都一样 —— 把前者混进后者，这条要求就永远立不住。

**没采到的量是 ``None``，不是 0。** 这条在本模块出现三次：代码统计的每一
项、测试结果整体、覆盖率整体。理由与基线表一样（``evaluation/baselines.py``）：
"没跑测试"与"跑了但一个都没通过"在报表上长得一模一样，含义相反。

**端口在领域层，实现在别处。** 领域层只说"我要一个 commit、一个代码统计、
一份测试结果、一份覆盖率"，怎么拿（git 命令、pytest 报告、coverage JSON）
不是领域的事 —— 实现在 ``harness/project/snapshot_probes.py``，与
``harness/sandbox/cancellation.py`` 实现 ``core/task/cancellation.py`` 的端口
同一套做法。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable


class SnapshotError(ValueError):
    """快照口径的错 —— **在写下去之前就报**。"""


#: 快照文档的格式版本 —— **文档变了要能认出来**。
SNAPSHOT_DOCUMENT_VERSION: Final[str] = "1"


# --------------------------------------------------------------------------
# 采集到的那几个数
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotArtifact:
    """快照引用的产物 —— **原始报告在哪儿**。

    **URI 是给人复查用的。** 快照里记的那些数，在快照之外还有一份原始报告
    （pytest 的 XML、coverage 的 JSON）—— 记下 URI，事后能回去核对那个数
    是不是真的。**没记 URI 的数只能信，不能核。**
    """

    #: 产物是哪一类 —— ``test_report`` / ``coverage_report`` 之类。
    kind: str
    #: 产物在哪儿。
    uri: str
    #: 内容的 sha256 —— **有就能验，没就只是"指个地方"**。
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise SnapshotError("产物的类别不能是空白")
        if not self.uri.strip():
            raise SnapshotError("产物的 URI 不能是空白")

    def to_document(self) -> dict[str, object]:
        """落盘的那份。"""
        return {"kind": self.kind, "uri": self.uri, "sha256": self.sha256}

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> SnapshotArtifact:
        """从落盘的那份重建。"""
        return cls(
            kind=_as_str(raw.get("kind"), where="产物的类别"),
            uri=_as_str(raw.get("uri"), where="产物的 URI"),
            sha256=_optional_str(raw.get("sha256"), where="产物的 hash"),
        )


@dataclass(frozen=True, slots=True)
class CodeStatistics:
    """代码统计 —— **每一项都可能没采到**。

    ``files`` / ``lines`` / ``modules`` 各自独立：采到了文件数不等于采到了
    行数。**没采到的那一项是 ``None``，不是 0** —— 0 个文件是一个事实
    （空仓库），"没数"是另一回事。
    """

    #: 源文件数。
    files: int | None = None
    #: 代码行数。
    lines: int | None = None
    #: 模块数（按顶层目录 / 包算）。
    modules: int | None = None

    def __post_init__(self) -> None:
        for name in ("files", "lines", "modules"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise SnapshotError(f"{name} 不能是负数")

    def to_document(self) -> dict[str, object]:
        """落盘的那份。"""
        return {"files": self.files, "lines": self.lines, "modules": self.modules}

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> CodeStatistics:
        """从落盘的那份重建。"""
        return cls(
            files=_optional_int(raw.get("files"), where="代码统计的文件数"),
            lines=_optional_int(raw.get("lines"), where="代码统计的行数"),
            modules=_optional_int(raw.get("modules"), where="代码统计的模块数"),
        )


@dataclass(frozen=True, slots=True)
class SuiteOutcome:
    """测试结果 —— **跑过了才有**。

    **跑挂了也是一份结果，照记。** 快照不是"成功时才有的东西"，它记的是
    "当时跑出来是什么样"。
    """

    #: 通过数。
    passed: int
    #: 失败数。
    failed: int
    #: 跳过数。
    skipped: int = 0
    #: 出错数（测试自己崩了，不算失败）。
    errors: int = 0
    #: 跑了多久（毫秒）。
    duration_ms: int | None = None
    #: 这份结果来自哪个产物。
    artifact: SnapshotArtifact | None = None

    def __post_init__(self) -> None:
        for name in ("passed", "failed", "skipped", "errors"):
            value = getattr(self, name)
            if value < 0:
                raise SnapshotError(f"{name} 不能是负数")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise SnapshotError("duration_ms 不能是负数")

    @property
    def total(self) -> int:
        """一共跑了多少个用例。"""
        return self.passed + self.failed + self.skipped + self.errors

    @property
    def green(self) -> bool:
        """是不是全过了。

        **跳过不算失败，但也不算通过。**
        """
        return self.failed == 0 and self.errors == 0

    def to_document(self) -> dict[str, object]:
        """落盘的那份。"""
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "artifact": self.artifact.to_document() if self.artifact else None,
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> SuiteOutcome:
        """从落盘的那份重建。"""
        artifact = raw.get("artifact")
        return cls(
            passed=_as_int(raw.get("passed"), where="测试结果的通过数"),
            failed=_as_int(raw.get("failed"), where="测试结果的失败数"),
            skipped=_as_int(raw.get("skipped"), where="测试结果的跳过数"),
            errors=_as_int(raw.get("errors"), where="测试结果的出错数"),
            duration_ms=_optional_int(raw.get("duration_ms"), where="测试结果的时长"),
            artifact=(
                SnapshotArtifact.from_document(_as_mapping(artifact, where="测试结果的产物"))
                if artifact is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """覆盖率 —— **整体与按模块**。

    **按模块分开是必须的。** 一个仓库的总覆盖率会掩盖问题：新增一个没测的
    模块，总覆盖率掉 0.3%，而那个模块其实是 0。按模块的那份数才是
    TASK-P1-042（``threshold = max(模块基线, 全局地板)``）的输入。

    **整体没量到是 ``None``。** 没有覆盖率文件的那次采集，写 0 就等于说
    "这个仓库一行都没被测到" —— 而真相是"没人量"。
    """

    #: 整体覆盖率（0 到 1）。
    overall: float | None = None
    #: 按模块的覆盖率。
    by_module: Mapping[str, float] = field(default_factory=dict)
    #: 这份覆盖率来自哪个产物。
    artifact: SnapshotArtifact | None = None

    def __post_init__(self) -> None:
        if self.overall is not None and not 0.0 <= self.overall <= 1.0:
            raise SnapshotError("整体覆盖率必须在 0 与 1 之间")
        for name, value in self.by_module.items():
            if not 0.0 <= value <= 1.0:
                raise SnapshotError(f"模块 {name} 的覆盖率必须在 0 与 1 之间")
        object.__setattr__(self, "by_module", MappingProxyType(dict(self.by_module)))

    def module_coverage(self, module: str) -> float | None:
        """某个模块的覆盖率 —— **没这个模块就是没有，不是 0**。"""
        return self.by_module.get(module)

    def to_document(self) -> dict[str, object]:
        """落盘的那份 —— **按模块的那份按名字排好序**。

        排序是为了让两份快照的文档能逐字节比较 —— 字典的插入顺序不该成为
        "这两份一样吗"的判据。
        """
        return {
            "overall": self.overall,
            "by_module": {name: self.by_module[name] for name in sorted(self.by_module)},
            "artifact": self.artifact.to_document() if self.artifact else None,
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> CoverageReport:
        """从落盘的那份重建。"""
        by_module_raw = _as_mapping(raw.get("by_module") or {}, where="按模块的覆盖率")
        by_module: dict[str, float] = {}
        for name, value in by_module_raw.items():
            by_module[name] = _as_float(value, where=f"模块 {name} 的覆盖率")
        artifact = raw.get("artifact")
        return cls(
            overall=_optional_float(raw.get("overall"), where="整体覆盖率"),
            by_module=by_module,
            artifact=(
                SnapshotArtifact.from_document(_as_mapping(artifact, where="覆盖率的产物"))
                if artifact is not None
                else None
            ),
        )


# --------------------------------------------------------------------------
# 快照本体
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """快照是谁的、哪一版、指向哪个 commit。"""

    #: 属于哪个项目。
    project_id: str
    #: 项目内单调递增的版本号。
    version: int
    #: 采集时点的 commit SHA。
    commit: str
    #: 采集时点的分支。
    branch: str | None = None

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise SnapshotError("快照必须属于某个项目")
        if self.version < 1:
            raise SnapshotError("快照版本号从 1 开始")
        if not self.commit.strip():
            raise SnapshotError("快照必须指向一个 commit —— 没有 commit 的快照指向不了任何代码")


@dataclass(frozen=True, slots=True)
class SnapshotMetrics:
    """采集时点的那些数 —— **没采到的是 ``None``**。"""

    #: 代码统计。
    code: CodeStatistics = CodeStatistics()
    #: 测试结果 —— 没跑测试就是 ``None``。
    tests: SuiteOutcome | None = None
    #: 覆盖率 —— 没量就是 ``None``。
    coverage: CoverageReport | None = None

    def to_document(self) -> dict[str, object]:
        """落盘的那份 —— **这是要进 ``metrics`` 字段的那一块**。"""
        return {
            "code": self.code.to_document(),
            "tests": self.tests.to_document() if self.tests else None,
            "coverage": self.coverage.to_document() if self.coverage else None,
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> SnapshotMetrics:
        """从落盘的那份重建。"""
        tests = raw.get("tests")
        coverage = raw.get("coverage")
        return cls(
            code=CodeStatistics.from_document(_as_mapping(raw.get("code") or {}, where="代码统计")),
            tests=(
                SuiteOutcome.from_document(_as_mapping(tests, where="测试结果"))
                if tests is not None
                else None
            ),
            coverage=(
                CoverageReport.from_document(_as_mapping(coverage, where="覆盖率"))
                if coverage is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectSnapshotRecord:
    """一份项目快照 —— **当时的状态，就这么多**。

    **"身份"与"指标"分开。** 身份是它是谁的、哪一版、指向哪个 commit；
    指标是采集时点的那几个数。分开不是为了好看 —— 落库时身份进各自的列
    （``version`` / ``commit`` / ``branch``），指标进 ``metrics`` 那个 JSON
    字段，一一对应，不重复也不遗漏。
    """

    #: 身份。
    identity: SnapshotIdentity
    #: 采集到的指标。
    metrics: SnapshotMetrics = SnapshotMetrics()
    #: 什么时候采的 —— **ISO 8601**。
    #:
    #: **它不是元数据。** 它记的是"查的那一刻"，而元数据记的是"这个 commit
    #: 当时是什么样" —— 两者混在一起，"同一 commit 两次一致"就永远不成立。
    collected_at: str | None = None

    @property
    def project_id(self) -> str:
        """属于哪个项目。"""
        return self.identity.project_id

    @property
    def version(self) -> int:
        """项目内的第几版。"""
        return self.identity.version

    @property
    def commit(self) -> str:
        """指向哪个 commit。"""
        return self.identity.commit

    @property
    def branch(self) -> str | None:
        """采集时点在哪个分支。"""
        return self.identity.branch

    def artifacts(self) -> tuple[SnapshotArtifact, ...]:
        """这份快照引用了哪些产物 —— **顺序是固定的**。

        顺序固定是为了让两份快照的文档能逐字节比较。
        """
        found: list[SnapshotArtifact] = []
        if self.metrics.tests is not None and self.metrics.tests.artifact is not None:
            found.append(self.metrics.tests.artifact)
        if self.metrics.coverage is not None and self.metrics.coverage.artifact is not None:
            found.append(self.metrics.coverage.artifact)
        return tuple(found)

    def metadata(self) -> Mapping[str, object]:
        """**决定"这个 commit 当时是什么样"的那几样** —— 不含采集时点。

        **这是"两次构建一致"要比的那一份。** 它只由采集结果决定：同一个
        commit 上采到的东西一样，元数据就一样。
        """
        return {
            "commit": self.commit,
            "branch": self.branch,
            "code": self.metrics.code.to_document(),
            "tests": self.metrics.tests.to_document() if self.metrics.tests else None,
            "coverage": self.metrics.coverage.to_document() if self.metrics.coverage else None,
        }

    def metadata_digest(self) -> str:
        """元数据的指纹 —— **两次构建一致，指纹就一致**。

        比一整份字典麻烦，比一个哈希麻烦；指纹两边都一样就够了。
        """
        payload = json.dumps(
            self.metadata(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_document(self) -> dict[str, object]:
        """落盘的那份 —— **凭它就能把当时的快照重建出来**。"""
        return {
            "document_version": SNAPSHOT_DOCUMENT_VERSION,
            "project_id": self.project_id,
            "version": self.version,
            "commit": self.commit,
            "branch": self.branch,
            **self.metrics_document(),
        }

    def metrics_document(self) -> dict[str, object]:
        """进 ``metrics`` 字段的那一块 —— **指标连同采集时点**。

        **采集时点跟指标放在一起，不是混为一谈。** 它不属于元数据（那是
        "两次构建要比的那一份"），但它属于**记录** —— 少了它，事后重建出来
        的快照就不知道自己是什么时候的了。
        """
        return {**self.metrics.to_document(), "collected_at": self.collected_at}

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> ProjectSnapshotRecord:
        """从落盘的那份重建 —— **不查任何别的地方**。

        **重建出来的东西必须和原来一样。** 这是"当时的快照可被事后重建"的
        落点：数字是从这份文档里读出来的，不是重新跑一遍测出来的。
        """
        identity = SnapshotIdentity(
            project_id=_as_str(raw.get("project_id"), where="快照的项目"),
            version=_as_int(raw.get("version"), where="快照的版本号"),
            commit=_as_str(raw.get("commit"), where="快照的 commit"),
            branch=_optional_str(raw.get("branch"), where="快照的分支"),
        )
        collected_at = _optional_str(raw.get("collected_at"), where="快照的采集时点")
        return cls(
            identity=identity,
            metrics=SnapshotMetrics.from_document(raw),
            collected_at=collected_at,
        )

    def metric(self, name: str) -> float | int | None:
        """取回当时记下来的那一个数。

        **只认快照里记下的** —— 不回头查、不重跑。取不到是 ``None``：那是
        "当时没采到"，不是"当时是 0"。
        """
        if name == "coverage":
            return self.metrics.coverage.overall if self.metrics.coverage else None
        if name == "tests_passed":
            return self.metrics.tests.passed if self.metrics.tests else None
        if name == "tests_failed":
            return self.metrics.tests.failed if self.metrics.tests else None
        if name == "files":
            return self.metrics.code.files
        if name == "lines":
            return self.metrics.code.lines
        if name == "modules":
            return self.metrics.code.modules
        raise SnapshotError(f"快照里没有这一项：{name}")


# --------------------------------------------------------------------------
# 采集口 —— 端口在领域层
# --------------------------------------------------------------------------


@runtime_checkable
class CommitProbe(Protocol):
    """取 commit 与分支 —— **代码在哪一版**。"""

    async def head_commit(self, workspace: Path) -> str:
        """当前 HEAD 的 commit SHA。"""
        ...

    async def current_branch(self, workspace: Path) -> str | None:
        """当前分支名 —— **游离 HEAD 时是 ``None``**。"""
        ...


@runtime_checkable
class CodeStatisticsProbe(Protocol):
    """数代码 —— **有多少文件、多少行、多少模块**。"""

    async def statistics(self, workspace: Path) -> CodeStatistics:
        """统计这个工作区的代码规模。"""
        ...


@runtime_checkable
class SuiteOutcomeProbe(Protocol):
    """取测试结果 —— **没跑过就是没有**。"""

    async def outcome(self, workspace: Path) -> SuiteOutcome | None:
        """这次的测试结果；没跑过（没有报告）返回 ``None``。"""
        ...


@runtime_checkable
class CoverageProbe(Protocol):
    """取覆盖率 —— **没量过就是没有**。"""

    async def coverage(self, workspace: Path) -> CoverageReport | None:
        """这次的覆盖率；没有覆盖率文件返回 ``None``。"""
        ...


@runtime_checkable
class SnapshotClock(Protocol):
    """取时间 —— **采集时点从这儿来，不从系统时钟直接读**。"""

    def now(self) -> datetime:
        """现在的时刻。"""
        ...


@dataclass(frozen=True, slots=True)
class SnapshotBuilder:
    """快照构建器 —— **采集一遍，落成一份记录**。

    **采集与记录分开。** 采集会起进程、读文件、可能失败；记录只是一份
    不可变的数据。分开之后，"能不能重建"与"采集时发生了什么"互不干扰。

    **构建器不重试、不补采。** 某个采集口拿不到东西，那一项就是 ``None`` ——
    快照如实记着"这次没采到"，而不是等采集齐了再记。**等齐了再记的快照
    已经不是"那个时点"的了**。
    """

    #: 取 commit 与分支。
    commits: CommitProbe
    #: 数代码。
    code: CodeStatisticsProbe
    #: 取测试结果。
    tests: SuiteOutcomeProbe
    #: 取覆盖率。
    coverage: CoverageProbe
    #: 取时间。
    clock: SnapshotClock

    async def build(
        self, *, project_id: str, version: int, workspace: Path
    ) -> ProjectSnapshotRecord:
        """采一遍，落成一份记录。"""
        commit = await self.commits.head_commit(workspace)
        branch = await self.commits.current_branch(workspace)
        code = await self.code.statistics(workspace)
        tests = await self.tests.outcome(workspace)
        coverage = await self.coverage.coverage(workspace)
        identity = SnapshotIdentity(
            project_id=project_id, version=version, commit=commit, branch=branch
        )
        return ProjectSnapshotRecord(
            identity=identity,
            metrics=SnapshotMetrics(code=code, tests=tests, coverage=coverage),
            collected_at=self.clock.now().isoformat(),
        )


# --------------------------------------------------------------------------
# 落盘时的取值 —— 类型不对要报错，不要默默塞个默认值
# --------------------------------------------------------------------------


def _as_str(raw: object, *, where: str) -> str:
    """取一个字符串 —— **类型不对就报错**。"""
    if not isinstance(raw, str):
        raise SnapshotError(f"{where} 必须是一个字符串")
    return raw


def _optional_str(raw: object, *, where: str) -> str | None:
    """取一个可省的字符串。"""
    if raw is None:
        return None
    return _as_str(raw, where=where)


def _as_int(raw: object, *, where: str) -> int:
    """取一个整数 —— **布尔不算整数**。

    ``True`` 是 ``int`` 的子类，不挡一下的话，"通过数是 True"会被当成
    "通过数是 1"，而那是一份被悄悄改过的报告。
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SnapshotError(f"{where} 必须是一个整数")
    return raw


def _optional_int(raw: object, *, where: str) -> int | None:
    """取一个可省的整数。"""
    if raw is None:
        return None
    return _as_int(raw, where=where)


def _as_float(raw: object, *, where: str) -> float:
    """取一个数 —— **布尔不算数**。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SnapshotError(f"{where} 必须是一个数")
    return float(raw)


def _optional_float(raw: object, *, where: str) -> float | None:
    """取一个可省的数。"""
    if raw is None:
        return None
    return _as_float(raw, where=where)


def _as_mapping(raw: object, *, where: str) -> Mapping[str, object]:
    """取一个映射。"""
    if not isinstance(raw, Mapping):
        raise SnapshotError(f"{where} 必须是一个映射")
    result: dict[str, object] = {}
    for key, value in raw.items():
        result[str(key)] = value
    return result


__all__ = [
    "SNAPSHOT_DOCUMENT_VERSION",
    "CodeStatistics",
    "CommitProbe",
    "CoverageProbe",
    "CoverageReport",
    "ProjectSnapshotRecord",
    "SnapshotArtifact",
    "SnapshotBuilder",
    "SnapshotClock",
    "SnapshotError",
    "SnapshotIdentity",
    "SnapshotMetrics",
    "SuiteOutcome",
    "SuiteOutcomeProbe",
]
