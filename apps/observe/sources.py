"""六路观察来源 —— 每一路都从**真的东西**里读出现状。

设计依据：docs/design.md §3 第一层（项目状态观察的六类数据来源：Git Commit
/ PR / 代码变更；单元与集成与 E2E 测试；静态代码分析与依赖漏洞与复杂度；
运行时性能与日志与异常与业务指标；产品需求与 Issue 与用户反馈；技术文档
与架构决策与设计规范）；docs/tasks.md TASK-P3-013（六类数据源各接入 ≥1 个
真实来源）；R-028 候选要有真实证据；R-029 证据必须可独立核验。

============  ==========================================  ====================
观察来源      读什么                                      证据落在哪
============  ==========================================  ====================
代码变更      真的 git 历史（哪个文件改得最频繁）          那个文件本身
测试          真的跑一遍 pytest，看哪条用例没过            那个用例所在的文件
静态分析      真的扫一遍仓库的依赖图                       那个模块所在的文件
运行时        真的跑一条命令，看它是不是崩了                那条命令看的文件
产品反馈      人写的报告台账                              台账文件 + 问题单号
技术文档      知识库里的决策记录                           那篇记录本身
============  ==========================================  ====================

**每一路都必须能落一个证据。** 这是 R-028 与 R-029 的硬要求，也是这一层最
容易踩空的地方：**运行那一路的探针如果没说是看哪个文件的，就产生不了观察**
—— 一条"命令崩了"的观察没有落点，就只是一句话。所以 :class:`RuntimeProbe`
把 ``evidence_path`` 写进了自己的字段里，而不是让人在事后补。

**"读不到"与"没有"是两件事。** 台账不在、目录不在、不是 git 仓库 —— 这些
都产出不了观察，但它们**不是错误**：这一层读的是"一个可能存在的东西"，
没有就当没有。**只有"跑不成"才是失败** —— 测试套件起不来、索引建不起来，
那是要有人去修的。

**观察只说事实，不说判断。** 这一层不说"这个文件是热点"，它说"这个文件在
最近 20 次提交里被改了 5 次"；不说"这里分层错了"，它说"产品代码 X 依赖了
测试代码 Y"。**判断留给发现层** —— 那边才知道一个事实值不值得变成候选。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apps.readers import (
    RuntimeProbe,
    read_changed_files,
    read_ledger,
    read_records,
    run_process,
    run_pytest,
)
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.observe import Observation, ObservationKind
from knowledge.architecture.model import ArchitectureModel, ViolationKind

#: 看最近多少次提交来算改动频率。**仓库默认值，不是实测值。**
CHURN_WINDOW: Final[int] = 20

#: 一个文件被改过几次才算"反复改动"。**仓库默认值，不是实测值。**
#:
#: **为什么是 2 而不是 1。** 改过一次是常态；改过两次以上才说明这块在反复
#: 折腾。**这个数是拍的** —— 真实的阈值要看项目的提交历史分布，而那个分布
#: 本仓库没测过。
CHURN_THRESHOLD: Final[int] = 2

#: 跑测试套件的超时上限（秒）。**给上限** —— 套件挂住了要报错。
TEST_SUITE_TIMEOUT_SECONDS: Final[float] = 120.0

#: pytest 里"跑完了，有用例没过"的退出码。其余非零退出码都是"没跑成"。
PYTEST_FAILED: Final[int] = 1


class SourceReadError(Exception):
    """这一路**没读成**。

    **名字带 ``Error`` 是仓库的硬规矩**（ruff 的 N818）。

    **"没读成"与"没有"必须分开。** 一个跑不起来的测试套件看起来和一个全绿
    的套件一模一样 —— 把它当成后者，就等于报了一句假结论。**缺实测值是失
    败，不是 0。**
    """


@dataclass
class ChangeHistorySource:
    """代码变更那一路 —— 真的 git 历史。

    :param root: 仓库根目录。
    :param window: 看最近多少次提交。
    :param threshold: 被改过几次才算反复改动。

    **它读的是"状态"而不是"事件"。** 触发层读的是"新来了哪几次提交"（每
    次提交是一件事）；这里读的是"哪个文件改得最频繁"（那是一件**关于这个
    文件**的事，反复成立）。**所以它没有去重窗口** —— 同一件事被反复观察到
    是对的。

    **不是 git 仓库就什么也不产出**，而不是报错 —— 没有 git 历史的项目，
    这一路就是空的。
    """

    root: Path
    window: int = CHURN_WINDOW
    threshold: int = CHURN_THRESHOLD

    @property
    def kind(self) -> ObservationKind:
        """这一路是代码变更观察。"""
        return ObservationKind.CHANGES

    def observe(self) -> tuple[Observation, ...]:
        """看哪个文件改得最频繁。"""
        entries = [
            (path, count)
            for path, count in read_changed_files(self.root, limit=self.window)
            if count >= self.threshold
        ]
        skipped = sum(1 for path, _ in entries if not _referenceable(path))
        observations: list[Observation] = []
        for path, count in entries:
            if not _referenceable(path):
                continue
            detail = f"改动次数={count}"
            if skipped:
                detail += f"；另有 {skipped} 个文件落不了证据，没被观察"
            observations.append(
                Observation(
                    kind=ObservationKind.CHANGES,
                    subject=path,
                    fact=f"这个文件在最近 {self.window} 次提交里被改了 {count} 次",
                    evidence=(Evidence(kind=EvidenceKind.FILE, ref=path),),
                    detail=detail,
                )
            )
        return tuple(observations)


@dataclass
class PytestSuiteSource:
    """测试那一路 —— 真的跑一遍 pytest。

    :param root: 仓库根目录。
    :param targets: **跑哪些东西。必须说清楚。**
    :param run_xfail: **把标记为 xfail 的用例当成真失败跑。**
    :param timeout: 超时上限。

    **为什么 ``targets`` 没有默认值。** "跑整个项目"在不同项目里指的是不同
    的目录 —— 试点仓库的缺陷复现套件在 ``known_issues/``，而它自己的回归
    套件在 ``tests/``。**跑错了地方得到的是"没有失败"，那是个假结论。**

    **为什么默认开 ``run_xfail``。** ``known_issues/`` 里放的是**已知缺陷的
    复现用例** —— 它们标着 ``xfail``，所以正常跑的时候它们是"预期中的失
    败"。但这一路要回答的是"**这些毛病还在吗**"，所以要按真失败来跑。

    **证据落在用例所在的文件上**，不在用例 id 上 —— 用例 id 里带着
    ``::``，它不是一个文件路径，而证据的形态是声明出来的（:mod:`core.
    evolution.evidence`）。用例 id 放在事实与细节里。
    """

    root: Path
    targets: tuple[str, ...]
    run_xfail: bool = True
    timeout: float = TEST_SUITE_TIMEOUT_SECONDS

    @property
    def kind(self) -> ObservationKind:
        """这一路是测试观察。"""
        return ObservationKind.TESTS

    def observe(self) -> tuple[Observation, ...]:
        """跑一遍，取出没过的用例。

        :raises SourceReadError: **测试套件没跑成** —— 那不是"没有失败"。
        """
        try:
            outcome = run_pytest(
                self.root,
                self.targets,
                run_xfail=self.run_xfail,
                timeout=self.timeout,
            )
        except RuntimeError as error:
            raise SourceReadError(str(error)) from error
        if outcome.returncode not in (0, PYTEST_FAILED):
            raise SourceReadError(
                f"在 {self.root} 里跑 pytest 没跑成（退出码 {outcome.returncode}）—— "
                "这不是'没有用例没过'，是这一轮没有测量结果"
            )
        observations: list[Observation] = []
        for node_id in outcome.failed_node_ids:
            observations.append(
                Observation(
                    kind=ObservationKind.TESTS,
                    subject=node_id,
                    fact=f"用例没通过：{node_id}",
                    evidence=(Evidence(kind=EvidenceKind.FILE, ref=_file_of(node_id)),),
                    detail=_counts_detail(outcome.counts),
                )
            )
        return tuple(observations)


@dataclass
class StaticAnalysisSource:
    """静态分析那一路 —— 真的扫一遍仓库的依赖图。

    :param architecture: 架构模型（由索引推导出来）。

    **它读的是索引，不是源码。** 依赖关系是索引里的事实 —— 跑一遍静态分析
    器要几秒，而索引是现成的。

    **两类结构问题各出一条观察。** 一条是"这条依赖不该存在"（分层违规），
    一条是"这几个模块兜圈子"（循环依赖）。**它们的落点不一样** —— 前者的
    证据是导入方的文件，后者是环上第一个模块的文件。

    **一个环只报一次。** 违规清单里也有循环依赖那一类，但**这里跳过它** ——
    否则同一个环会被报两遍：一条是"a 依赖了 b"，一条是"a 与 b 兜圈子"。
    **两遍就是两批候选，候选 id 还会撞车** —— 撞车的那个只能被丢掉，丢掉
    的那个理由会让人以为"这条观察有问题"，其实它只是重复了。**环那一遍说
    的更全**（把环上所有的模块都列出来），所以留它。

    **没有就什么也不产出** —— 一个没有违规也没有环的项目，这一路是空的，
    不是失败的。
    """

    architecture: ArchitectureModel

    @property
    def kind(self) -> ObservationKind:
        """这一路是静态分析观察。"""
        return ObservationKind.STATIC

    def observe(self) -> tuple[Observation, ...]:
        """看依赖图里有没有说不通的地方。"""
        observations: list[Observation] = []
        for violation in self.architecture.violations():
            if violation.kind is ViolationKind.CYCLIC_DEPENDENCY:
                continue
            path = self._path_of(violation.importer)
            if path is None:
                continue
            observations.append(
                Observation(
                    kind=ObservationKind.STATIC,
                    subject=f"{violation.importer} -> {violation.imported}",
                    fact=violation.message,
                    evidence=(Evidence(kind=EvidenceKind.FILE, ref=path),),
                    detail=violation.describe(),
                )
            )
        for cycle in self.architecture.cycles():
            path = self._path_of(cycle.modules[0])
            if path is None:
                continue
            observations.append(
                Observation(
                    kind=ObservationKind.STATIC,
                    subject=" -> ".join(cycle.modules),
                    fact=f"这几个模块形成了循环依赖：{cycle.describe()}",
                    evidence=(Evidence(kind=EvidenceKind.FILE, ref=path),),
                    detail=cycle.describe(),
                )
            )
        return tuple(observations)

    def _path_of(self, module: str) -> str | None:
        """模块所在的文件（仓库相对路径）。索引里没有就返回 ``None``。"""
        record = self.architecture.index.record(module)
        return record.path if record is not None else None


@dataclass
class RuntimeMetricSource:
    """运行时那一路 —— 真的跑一条命令，看它是不是崩了。

    :param probes: 要看着的命令。

    **只报事故，不报正常。** 一条跑通了的命令不产出观察 —— "它跑通了"不是一
    个待办。**性能观测要基线与阈值**，那是另一件事（见 docs/architecture/
    opportunity-pipeline.md 的"没测的"一节）。

    **没有落点的探针产生不了观察。** :class:`~apps.readers.RuntimeProbe` 里
    的 ``evidence_path`` 说的是"这条命令看的是哪个文件"；没写就**没有证
    据**，而没有证据的观察不是观察。**这一条是刻意的**：宁可少报一件事，
    也不要一条没落点的话。
    """

    probes: tuple[RuntimeProbe, ...]

    @property
    def kind(self) -> ObservationKind:
        """这一路是运行时观察。"""
        return ObservationKind.RUNTIME

    def observe(self) -> tuple[Observation, ...]:
        """跑一遍探针，把出事的记下来。"""
        observations: list[Observation] = []
        for probe in self.probes:
            if not probe.evidence_path:
                continue
            fact = _incident_of(probe)
            if fact is None:
                continue
            observations.append(
                Observation(
                    kind=ObservationKind.RUNTIME,
                    subject=probe.id,
                    fact=fact,
                    evidence=(Evidence(kind=EvidenceKind.FILE, ref=probe.evidence_path),),
                    detail=" ".join(probe.argv),
                )
            )
        return tuple(observations)


@dataclass
class FeedbackSource:
    """产品反馈那一路 —— 人写的报告台账。

    :param root: 仓库根目录。
    :param ledger: 台账在仓库里的相对路径。

    **台账是人写的。** 所以解析只认一种形状（``### <编号> —— <标题>``），
    认不出来的那条就跳过 —— **宁可漏报，不要报错**。

    **两条证据：台账文件与问题单号。** 只给一条都不够 —— 只给文件，那是
    "某个文件里有一段话"，核验的人得自己去找是哪一条；只给编号，那是
    "某个编号"，核验的人得自己去找它在哪。两个都给了，才能当场核上。
    """

    root: Path
    ledger: Path

    @property
    def kind(self) -> ObservationKind:
        """这一路是产品反馈观察。"""
        return ObservationKind.FEEDBACK

    def observe(self) -> tuple[Observation, ...]:
        """读台账。"""
        ledger_path = self.root / self.ledger
        observations: list[Observation] = []
        for entry in read_ledger(ledger_path):
            observations.append(
                Observation(
                    kind=ObservationKind.FEEDBACK,
                    subject=entry.id,
                    fact=entry.title,
                    evidence=(
                        Evidence(kind=EvidenceKind.ISSUE, ref=entry.id),
                        Evidence(kind=EvidenceKind.FILE, ref=_relative(self.ledger)),
                    ),
                )
            )
        return tuple(observations)


@dataclass
class DocumentationSource:
    """技术文档那一路 —— 知识库里的决策记录。

    :param root: 仓库根目录。
    :param directory: 记录目录在仓库里的相对路径。

    **这一类观察不产出候选** —— 决策记录写的是当初已经做过的决定，不是待
    办。**但它仍然要被读到**：六类数据源都得接上，而"接上"与"产出候选"是
    两件事。

    **证据落在记录文件本身。** 一篇 ADR 的存在就是它自己的证据。
    """

    root: Path
    directory: Path

    @property
    def kind(self) -> ObservationKind:
        """这一路是技术文档观察。"""
        return ObservationKind.DOCS

    def observe(self) -> tuple[Observation, ...]:
        """读记录。"""
        observations: list[Observation] = []
        for entry in read_records(self.root / self.directory):
            observations.append(
                Observation(
                    kind=ObservationKind.DOCS,
                    subject=entry.stem,
                    fact=entry.title or entry.stem,
                    evidence=(
                        Evidence(
                            kind=EvidenceKind.FILE,
                            ref=_relative(self.directory / f"{entry.stem}.md"),
                        ),
                    ),
                )
            )
        return tuple(observations)


def _file_of(node_id: str) -> str:
    """用例 id 里的文件部分。

    **用例 id 长这样：**``known_issues/test_x.py::test_case``。证据的形态是
    声明出来的（文件路径），所以这里取文件那一段 —— 用例 id 放在事实里。
    """
    return node_id.split("::", 1)[0]


def _referenceable(path: str) -> bool:
    """这个路径能不能当证据引用。

    **与证据词表同一套判据**（:func:`core.evolution.evidence._looks_like_path`
    那一套：带分隔符、点开头、或者带扩展名）。**这里要自己判一遍**，是因为
    词表那一层是私有的，而这一层得在**造证据之前**就知道答案 —— 造不出来
    的东西不能硬造。

    **代价说清楚**：名字不带扩展名、又在仓库根目录下的文件（``Makefile``、
    ``LICENSE``）跟一句自然语言描述分不开，词表不收它们，所以这一路也看
    不到它们。**这是词表的边界，不是"这里没有热点"。** 观察里会写明另有
    几个文件被跳过。
    """
    if "/" in path or "\\" in path:
        return True
    if any(char.isspace() for char in path):
        return False
    return path.startswith(".") or "." in path


def _counts_detail(counts: Mapping[str, int]) -> str:
    """把 pytest 的计数写成一句话。"""
    if not counts:
        return "这一轮没有读到计数"
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _incident_of(probe: RuntimeProbe) -> str | None:
    """跑一条探针。**出了事返回一句人话，没出事返回 ``None``。**

    **三态都分得清**：超时、起不来、退出码非零 —— 报给上层的话不一样，补救
    动作也不一样。
    """
    outcome = run_process(probe.argv, cwd=probe.cwd, timeout=probe.timeout, env=dict(probe.env))
    if outcome.timed_out:
        return f"命令 {probe.id} 超时（超过 {probe.timeout} 秒）"
    if not outcome.started:
        return f"命令 {probe.id} 跑不起来：{outcome.error}"
    if outcome.returncode == 0:
        return None
    tail = (outcome.stderr or outcome.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else "没有输出"
    return f"命令 {probe.id} 退出码 {outcome.returncode}：{detail}"


def _relative(path: Path) -> str:
    """仓库相对路径 —— **用正斜杠**。

    **证据引用里的路径必须是仓库相对路径**（核验器拿它去根目录下找）。用正
    斜杠是因为那是仓库里记路径的样子，写成反斜杠就成了一种**在 Windows 上
    才对**的引用。
    """
    return path.as_posix()


__all__ = [
    "CHURN_THRESHOLD",
    "CHURN_WINDOW",
    "PYTEST_FAILED",
    "TEST_SUITE_TIMEOUT_SECONDS",
    "ChangeHistorySource",
    "DocumentationSource",
    "FeedbackSource",
    "PytestSuiteSource",
    "RuntimeMetricSource",
    "SourceReadError",
    "StaticAnalysisSource",
]
