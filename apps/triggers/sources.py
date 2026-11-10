"""六路触发源 —— 每一路都从**真实的东西**里读出事件。

设计依据：docs/tasks.md TASK-P3-011（交付物：定时 / 代码 / 质量 / 运行 / 外部
/ 知识 六类触发器；验收条件"六类事件各能触发一轮演进；同一事件去重后只触发
一次"）、R-079、R-080、I-11；docs/design.md §4（六个触发源）。

**每一路读的东西都是真的，不是编出来的。**

============  ==================================  ====================
触发源        读什么                                事件的主体
============  ==================================  ====================
定时          项目索引（扫一遍看有没有新问题）        项目
代码          真的 git 仓库的提交历史                提交 sha
质量          真的跑一遍 pytest，看哪条用例没过      失败的用例 id
运行          真的跑一条命令，看它是不是崩了          探针 id
外部          人写的报告台账                        报告 id
知识          知识库里的决策记录                    记录 id
============  ==================================  ====================

**"运行"与"质量"的分界。** 质量是**代码的毛病**（一条用例没过）；运行是
**跑起来之后出的事**（进程崩了、超时了、退出码非零）。同一个失败从两个角
度看是两件事：质量那一路告诉你"哪一条检查没过"，运行那一路告诉你"整个
流程没能跑完"。**把两者混成一路，就会丢掉"整个流程挂了"这个信息。**

**生产者是纯粹的，重复由去重窗口兜住。** 每一路 ``collect()`` 返回"现在这
个来源上有哪些事"，**不记自己上次读到哪** —— 所以同一路连着 collect 两次
会返回同样的事件，**这是对的**：去重窗口（:mod:`orchestration.dedup`）就是
为这件事存在的，生产者自己再记一份游标只会多一处会忘的地方。

**每一路都有边界，说在明处。**

- **知识那一路接的是架构决策记录（ADR）**，不是依赖漏洞扫描 —— 后者要一个
  依赖漏洞数据库，本仓库没有。**这不是"知识触发"只能这样**，是"目前接的
  是这一种知识"；再接一种只要再写一个同样的适配器。
- **质量那一路跑的是真的 pytest**，所以它慢（几百毫秒到几秒）。它不该在
  请求路径上跑，只该在定时任务里跑。
- **外部那一路认一种台账格式**（``### <编号> —— <标题>``）。认不出来的那条
  就跳过 —— **宁可漏报，不要报错**：台账是人写的，格式变一点不该让整个系
  统罢工。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from apps.readers import (
    FAILED_LINE,
    REPORT_HEADING,
    RuntimeProbe,
    read_commits,
    read_ledger,
    read_records,
    run_process,
    run_pytest,
)
from apps.scheduler.scan_job import ScanHistory, fresh_signals
from core.evolution.evolution_loop import TriggerSource
from core.evolution.scan import ScanScope, ScanSignal
from core.evolution.triggers import TriggerEvent, TriggerEventType
from orchestration.scan import ProjectScanner

#: 质量那一路跑 pytest 时用的超时。**给上限** —— 测试套件挂住了要报错，
#: 不能一直等着。
QUALITY_TIMEOUT_SECONDS: Final[float] = 120.0

#: 运行那一路跑命令时用的超时（探针自己也可以定一个更短的）。
RUNTIME_TIMEOUT_SECONDS: Final[float] = 60.0


@runtime_checkable
class TriggerSourcePort(Protocol):
    """一类触发源 —— 产出触发事件。

    **"产出"是纯读。** 它不判断该不该触发，也不去重 —— 那两件事在派发
    那一侧统一做。
    """

    @property
    def source(self) -> TriggerSource:
        """这一路是哪一类触发源。"""
        ...

    def collect(self) -> tuple[TriggerEvent, ...]:
        """现在这个来源上有哪些事。"""
        ...


@dataclass
class ScanScheduleSource:
    """定时触发源 —— 定期扫一遍项目。

    :param scanner: 扫描器。
    :param scope: 扫描范围。
    :param history: 扫描历史 —— 用来判断哪些信号是新的。
    :param scan_id_prefix: 扫描 id 的前缀。
    :param clock: 现在的时刻。
    :param project_id: 属于哪个项目。**不给就用范围里的。**

    **它与 :class:`apps.scheduler.scan_job.ScheduledScanner` 用的是同一套
    扫描器与同一套历史。** 区别只在于出口：那个直接派发，这个只产出事件。

    **一轮扫描最多产出一条事件。** 扫出来的新问题有几个不重要 —— 重要的
    是"这个项目又有新问题了"，所以主体是项目而不是某一个信号。**一条事件
    一个项目**也是去重窗口能挡住"天天扫、天天开轮"的原因。
    """

    scanner: ProjectScanner
    scope: ScanScope
    history: ScanHistory
    scan_id_prefix: str = "scan"
    clock: Callable[[], datetime] | None = None
    project_id: str | None = None
    _counter: int = field(default=0, init=False)

    @property
    def source(self) -> TriggerSource:
        """这一路是定时触发。"""
        return TriggerSource.SCHEDULE

    def collect(self) -> tuple[TriggerEvent, ...]:
        """扫一遍，看有没有新问题。"""
        self._counter += 1
        scan_id = f"{self.scan_id_prefix}-{self._counter}"
        _, fresh = fresh_signals(
            self.scanner,
            self.scope,
            self.history,
            scan_id=scan_id,
            at=self._now(),
        )
        if not fresh:
            return ()
        return (
            TriggerEvent(
                source=TriggerSource.SCHEDULE,
                event_type=TriggerEventType.SCHEDULE_SCAN,
                aggregate_id=self._project_id(),
                project_id=self._project_id(),
                summary=_summary_of_scan(fresh),
            ),
        )

    def _now(self) -> datetime:
        """现在。"""
        if self.clock is not None:
            return self.clock()
        return datetime.now(UTC)

    def _project_id(self) -> str:
        """这个扫描属于哪个项目。"""
        if self.project_id is not None:
            return self.project_id
        return self.scope.project_id


@dataclass
class GitCommitSource:
    """代码触发源 —— 真的 git 仓库的提交。

    :param repo: 仓库根目录。
    :param since: 只看这个提交之后的。
    :param limit: 最多看多少条。
    :param project_id: 属于哪个项目。**不给就用仓库目录名。**

    **主体是提交 sha。** 一次提交是一件事 —— 同一次提交被报十遍是一件事，
    两次不同的提交是两件事。**这个粒度直接决定去重窗口挡不挡得住。**

    **不是 git 仓库就什么也不产出**，而不是报错 —— 这一路读的是"一个可能
    存在的仓库"，没有就当没有。
    """

    repo: Path
    since: str | None = None
    limit: int = 20
    project_id: str | None = None

    @property
    def source(self) -> TriggerSource:
        """这一路是代码事件。"""
        return TriggerSource.CODE

    def collect(self) -> tuple[TriggerEvent, ...]:
        """读提交历史。"""
        entries = read_commits(
            self.repo,
            limit=self.limit,
            since=self.since,
            timeout=RUNTIME_TIMEOUT_SECONDS,
        )
        events: list[TriggerEvent] = []
        for entry in entries:
            events.append(
                TriggerEvent(
                    source=TriggerSource.CODE,
                    event_type=TriggerEventType.CODE_COMMIT,
                    aggregate_id=entry.sha,
                    project_id=self._project_id(),
                    summary=entry.subject or entry.sha,
                )
            )
        return tuple(events)

    def _project_id(self) -> str:
        """这个仓库属于哪个项目。"""
        if self.project_id is not None:
            return self.project_id
        return self.repo.name


class QualitySourceError(Exception):
    """质量那一路**没跑成**。

    **名字带 ``Error`` 是仓库的硬规矩**（ruff 的 N818）—— 异常的名字要能
    在 traceback 里一眼看出是"哪一类问题"。

    **"没验成"与"验了没过"必须分开。** pytest 退出码 1 是"跑完了，有用例
    没过"；其他非零退出码（2 收集出错、3 内部错误、4 用法错、5 没收集到
    用例）都是"**根本没跑成**"。把后者当成前者，就会把一个跑不起来的测试
    套件报成"质量没问题"—— **那是个假结论**。
    """


#: pytest 的退出码 —— **说得出每个码是什么意思**。
#:
#: 只有 1 是"跑了，有用例没过"；0 是"跑了，都过了"。其余都是"没跑成"。
PYTEST_FAILED: Final[int] = 1
PYTEST_INTERRUPTED: Final[int] = 2
PYTEST_INTERNAL_ERROR: Final[int] = 3
PYTEST_USAGE_ERROR: Final[int] = 4
PYTEST_NO_TESTS_COLLECTED: Final[int] = 5


@dataclass
class PytestFailureSource:
    """质量触发源 —— 真的跑一遍 pytest，看哪条用例没过。

    :param repo: 仓库根目录。
    :param targets: **跑哪些东西。必须说清楚。**
    :param run_xfail: **把标记为 xfail 的用例当成真失败跑。**
    :param project_id: 属于哪个项目。

    **为什么``targets`` 没有默认值。** "跑整个项目"在不同项目里指的是不同
    的目录 —— 试点仓库的缺陷复现套件在 ``known_issues/``，而它自己的回归
    套件在 ``tests/``。**跑错了地方得到的是"没有失败"，那是个假结论。**
    所以要跑什么由调用方说。

    **为什么默认开 ``run_xfail``。** ``known_issues/`` 里放的是**已知缺陷的
    复现用例** —— 它们标着 ``xfail``，所以正常跑的时候它们是"预期中的失
    败"。但一份质量台账要回答的是"**这些毛病还在吗**"，所以要按真失败来
    跑、看它们是不是真的失败了。**跑出 ``XPASS`` 说明缺陷已经不存在了** ——
    那是好消息，但它不是"质量事件"。
    """

    repo: Path
    targets: tuple[str, ...]
    run_xfail: bool = True
    project_id: str | None = None

    @property
    def source(self) -> TriggerSource:
        """这一路是质量事件。"""
        return TriggerSource.QUALITY

    def collect(self) -> tuple[TriggerEvent, ...]:
        """跑一遍，取出失败的用例。

        :raises QualitySourceError: **测试套件没跑成** —— 那不是"没有失败"。

        **退出码 1 才算"验了，没过"。** 其余非零退出码都意味着这一轮根本没
        有测量结果 —— 按本仓库的规矩，"缺实测值"是失败，不是 0。
        """
        try:
            outcome = run_pytest(
                self.repo,
                self.targets,
                run_xfail=self.run_xfail,
                timeout=QUALITY_TIMEOUT_SECONDS,
            )
        except RuntimeError as error:
            raise QualitySourceError(str(error)) from error
        if outcome.returncode not in (0, PYTEST_FAILED):
            raise QualitySourceError(
                f"在 {self.repo} 里跑 pytest 没跑成（退出码 {outcome.returncode}）—— "
                "这不是'没有用例没过'，是这一轮没有测量结果"
            )
        events: list[TriggerEvent] = []
        for node_id in outcome.failed_node_ids:
            events.append(
                TriggerEvent(
                    source=TriggerSource.QUALITY,
                    event_type=TriggerEventType.QUALITY_FAILURE,
                    aggregate_id=node_id,
                    project_id=self._project_id(),
                    summary=f"用例没通过：{node_id}",
                )
            )
        return tuple(events)

    def _project_id(self) -> str:
        """这个仓库属于哪个项目。"""
        if self.project_id is not None:
            return self.project_id
        return self.repo.name


@dataclass
class RuntimeProbeSource:
    """运行触发源 —— 真的跑一条命令，看它是不是崩了。

    :param probes: 要看着的命令。
    :param project_id: 属于哪个项目。**不给就用探针自己的工作目录名。**

    **崩了就是崩了，不解释。** 退出码非零、或者超时了，都是事故 —— 具体是
    什么原因，由演进循环去诊断（那是 P2-015 的事）。这一层只负责**如实说
    "它没跑成"**。
    """

    probes: tuple[RuntimeProbe, ...]
    project_id: str | None = None

    @property
    def source(self) -> TriggerSource:
        """这一路是运行事件。"""
        return TriggerSource.RUNTIME

    def collect(self) -> tuple[TriggerEvent, ...]:
        """跑一遍探针，把没跑成的记下来。"""
        events: list[TriggerEvent] = []
        for probe in self.probes:
            outcome = _run_probe(probe)
            if outcome is None:
                continue
            events.append(
                TriggerEvent(
                    source=TriggerSource.RUNTIME,
                    event_type=TriggerEventType.RUNTIME_INCIDENT,
                    aggregate_id=probe.id,
                    project_id=self._project_id(probe),
                    summary=outcome,
                )
            )
        return tuple(events)

    def _project_id(self, probe: RuntimeProbe) -> str:
        """这个探针属于哪个项目。"""
        if self.project_id is not None:
            return self.project_id
        if probe.cwd is not None:
            return probe.cwd.name
        return probe.id


@dataclass
class ReportInboxSource:
    """外部触发源 —— 人报上来的问题。

    :param path: 报告台账（Markdown）。
    :param project_id: 这些报告属于哪个项目。

    **台账是人写的。** 所以解析只认一种形状（``### <编号> —— <标题>``），
    认不出来的那条就跳过 —— **宁可漏报，不要报错**。
    """

    path: Path
    project_id: str = "unknown"

    @property
    def source(self) -> TriggerSource:
        """这一路是外部事件。"""
        return TriggerSource.EXTERNAL

    def collect(self) -> tuple[TriggerEvent, ...]:
        """读台账。"""
        events: list[TriggerEvent] = []
        for entry in read_ledger(self.path):
            events.append(
                TriggerEvent(
                    source=TriggerSource.EXTERNAL,
                    event_type=TriggerEventType.EXTERNAL_REPORT,
                    aggregate_id=entry.id,
                    project_id=self.project_id,
                    summary=entry.title,
                )
            )
        return tuple(events)


@dataclass
class KnowledgeRecordSource:
    """知识触发源 —— 知识库里多了一条记录。

    :param directory: 记录放在哪。
    :param project_id: 属于哪个项目。

    **接的是架构决策记录（ADR）。** 依赖漏洞扫描是同一类事的另一种来源，
    要接进来只要再写一个适配器 —— **这一层不假装自己接了它**。
    """

    directory: Path
    project_id: str = "unknown"

    @property
    def source(self) -> TriggerSource:
        """这一路是知识事件。"""
        return TriggerSource.KNOWLEDGE

    def collect(self) -> tuple[TriggerEvent, ...]:
        """读记录。"""
        events: list[TriggerEvent] = []
        for entry in read_records(self.directory):
            events.append(
                TriggerEvent(
                    source=TriggerSource.KNOWLEDGE,
                    event_type=TriggerEventType.KNOWLEDGE_UPDATE,
                    aggregate_id=entry.stem,
                    project_id=self.project_id,
                    summary=entry.title or entry.stem,
                )
            )
        return tuple(events)


def _run_probe(probe: RuntimeProbe) -> str | None:
    """跑一条探针。跑成了返回 ``None``，没跑成返回一句人话。

    **三态都分得清**：超时、起不来、退出码非零 —— 报给上层的话不一样，
    补救动作也不一样。
    """
    outcome = run_process(
        probe.argv,
        cwd=probe.cwd,
        timeout=probe.timeout,
        env=dict(probe.env),
    )
    if outcome.timed_out:
        return f"命令 {probe.id} 超时（超过 {probe.timeout} 秒）"
    if not outcome.started:
        return f"命令 {probe.id} 跑不起来：{outcome.error}"
    if outcome.returncode == 0:
        return None
    tail = (outcome.stderr or outcome.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else "没有输出"
    return f"命令 {probe.id} 退出码 {outcome.returncode}：{detail}"


def _summary_of_scan(signals: tuple[ScanSignal, ...]) -> str:
    """一轮扫描产出的事件，一句话说明。"""
    kinds = sorted({item.kind.value for item in signals})
    return f"扫到 {len(signals)} 个新问题（{', '.join(kinds)}）"


__all__ = [
    "FAILED_LINE",
    "PYTEST_FAILED",
    "PYTEST_INTERNAL_ERROR",
    "PYTEST_INTERRUPTED",
    "PYTEST_NO_TESTS_COLLECTED",
    "PYTEST_USAGE_ERROR",
    "QUALITY_TIMEOUT_SECONDS",
    "REPORT_HEADING",
    "RUNTIME_TIMEOUT_SECONDS",
    "GitCommitSource",
    "KnowledgeRecordSource",
    "PytestFailureSource",
    "QualitySourceError",
    "ReportInboxSource",
    "RuntimeProbe",
    "RuntimeProbeSource",
    "ScanScheduleSource",
    "TriggerSourcePort",
]
