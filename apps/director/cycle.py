"""自主闭环的**通用驱动器** —— M1 与 M2 共用的那一段。

设计依据：docs/tasks.md TASK-P1-081（M1：手工注入一个 Candidate，走完 决策 →
任务 → 执行 → 独立验证 → 记录 全链）与 TASK-P1-082（M2：只给目标不给任务，
系统自主走完一轮）；docs/design.md §4（循环 B 的节点链与循环 A 的承接）与
§9 阶段一交付物。

**这一层放的是"每一轮都一样"的那部分。** 观察 → 快照 → 差距 → 发现 → 证据
→ 评估 → 决策 → 计划 → 拆解落库 → 排期 → 循环 A（工作区 → 执行 → **独立
验证引擎真跑 pytest** → 验收契约判定 → 合并闸门 → 回写）→ 项目知识更新，
每一步都经由编排层（:mod:`orchestration.loop_b` / :mod:`orchestration.loop_a`），
每一步都往数据库写一行。

**一轮与另一轮不一样的部分，装在 :class:`CycleRecipe` 里。** 谁观察、谁发现
候选、怎么评分、谁更新差距 —— 这些是"这一轮的脑子"：M1 的脑子是手工注入的
那一条候选（:mod:`apps.director.m1_cycle`），M2 的脑子是自主发现
（:mod:`apps.director.m2_discovery`）。**换脑子不换链路** —— 两轮走的是同一
张图、同一套闸门、同一套落库。

**只有一处是替换掉的：写代码的那一段。** 阶段一的执行器是本地补丁器
（:mod:`apps.director.m1_executor` / :mod:`apps.director.m2_executor`），不联网、
不调模型。理由见那两个模块的文档：链路上需要判断的地方全是真的，把"写代码"
也交给真模型只会让"这条链能不能跑通"取决于环境有没有 API 凭据。

**这一层为什么放在 ``apps/``。** 它同时要用编排层（``orchestration``）、
工作空间与验证引擎（``harness``）和落库层（``infrastructure``）—— 只有
``apps/`` 是唯一不受导入禁令约束的顶层。编排层自己不许 import 执行器，
工作空间层不许 import 编排层，这条禁令正是把驱动放在外面的理由。

**要一个真的 Postgres。** 不是为了"看起来像生产"，而是因为验收口径是
"每一步的 DB 记录可查" —— 而"可查"这件事要求那些记录**真的**在数据库里，
而不是在内存里假装存在。链路里唯一被替换掉的是"写代码"，不是"落库"。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.critic.approval import approval_for
from agents.critic.report import Critic, CriticReport
from agents.critic.sources import (
    GitHistoryTool,
    ProjectStatusTool,
    RepositorySearchTool,
    TaskTableTool,
    TestReportTool,
)
from agents.critic.tools import FactChecker, TaskSummary
from apps.director.patching import run_git
from core.entities.project import Project as ProjectRow
from core.evidence.artifacts import ArtifactKind, ArtifactRecord, VerificationRunRecord
from core.evolution.candidate import EvolutionCandidate
from core.evolution.decision import DecisionPolicy, DecisionRound, DecisionState
from core.evolution.evolution_loop import (
    EvolutionJournalPort,
    EvolutionPlan,
    LoopBState,
    ResourceAssignment,
    ResourceSchedule,
    TriggerSource,
)
from core.metrics.cycle import CycleStatus
from core.planning.roadmap import DecisionApproval, EvolutionRoadmap, TaskDraft, TaskType
from core.policy.autonomy import Operation, PolicyManager, TaskContext
from core.project.models import MetricType, Project, TargetValue, ValueKind, load_project
from core.task.execution_loop import (
    CollectedArtifacts,
    EventRecord,
    ExecutionPlan,
    FailureCategory,
    FailureDiagnosis,
    LoopAState,
    MergeOutcome,
    SessionRecord,
    WorkspaceInfo,
)
from core.task.outcome import VerificationReport, VerificationVerdict
from core.task.repair import InMemoryRepairLedger
from core.task.state_machine import TaskAction, TaskState
from executors.base import ExecutionResult
from harness.approval.gate import ApprovalGate
from harness.verification.contract import AcceptanceContract, ForbiddenRule, RequiredCheck
from harness.verification.engine import (
    CheckSpec,
    ScanSpec,
    VerificationEngine,
    VerificationPlan,
    VerificationRequest,
    VerificationRun,
)
from harness.workspace.git import SubprocessGitClient
from harness.workspace.manager import WorkspaceManager
from harness.workspace.models import SystemClock
from infrastructure.database.approval import PostgresApprovalStore
from infrastructure.database.artifact import PostgresArtifactRegistry
from infrastructure.database.evolution_journal import EvolutionJournal
from infrastructure.database.execution_journal import DatabaseExecutionJournal
from infrastructure.database.repositories import SnapshotRepository, TaskRepository
from orchestration.loop_a import ExecutorPort, LoopAHandlers
from orchestration.loop_b import (
    CandidateDiscovererPort,
    CandidateEvaluatorPort,
    GapAnalyzerPort,
    GoalGapUpdaterPort,
    LoopAEngineeringCycle,
    LoopBHandlers,
    PolicyDecisionMaker,
    ProjectObserverPort,
    build_loop_b_graph,
    run_loop_b,
)
from orchestration.repair import DEFAULT_LIMITS, RepairCoordinator

# --------------------------------------------------------------------------
# 常量与契约模板
# --------------------------------------------------------------------------

#: 主分支名。复制出来的仓库就建在这条分支上。
MAIN_BRANCH: Final[str] = "main"


@dataclass(frozen=True, slots=True)
class ContractTemplate:
    """验收契约的模板 —— **不含 task_id**。

    **为什么单独一个模板。** 契约必须写明它属于哪个任务，而任务 id 要等
    任务落库才知道。把"要什么"与"是谁的"分开，前者可以写死在代码里、可以
    被审，后者在落库那一刻填上。
    """

    required: tuple[RequiredCheck, ...] = (RequiredCheck.UNIT_TESTS_PASS, RequiredCheck.LINT_PASS)
    forbidden: tuple[ForbiddenRule, ...] = (ForbiddenRule.SECRETS_IN_DIFF,)
    artifacts: tuple[ArtifactKind, ...] = (
        ArtifactKind.TEST_REPORT,
        ArtifactKind.EXECUTION_TRACE,
    )

    def for_task(self, task_id: str) -> AcceptanceContract:
        """造出绑在这个任务上的那份契约。"""
        return AcceptanceContract(
            task_id=task_id,
            required=self.required,
            forbidden=self.forbidden,
            artifacts=self.artifacts,
        )


# --------------------------------------------------------------------------
# 配置、配方与结果
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CycleConfig:
    """这一轮的配置 —— **与"这一轮想干什么"无关的那部分**。

    :param project_file: 项目目标 YAML。
    :param pilot_source: 仓库源码目录 —— **会被复制成一份独立的 git 仓库**，
        原目录一个字都不动。
    :param work_root: 工作根目录。复制出来的仓库、工作树与测试报告都在它下面。
    :param python_executable: 跑测试与 lint 用的解释器。**写死成绝对路径** ——
        验证进程的环境变量是白名单造的，``PATH`` 未必指向同一个虚拟环境，
        用相对命令会跑起另一个 Python。
    :param project_id: 项目 id。**留空就取目标 YAML 里那一个** —— 项目 id 是
        目标的一部分，不该在代码里写死一份。
    :param cycle_id: 这一轮的周期 id。**留空就每次跑造一个新的**（见
        :func:`new_cycle_id`）—— 同一个周期 id 只能开一次。
    :param executor_name: 执行器名字。**留空就取配方里那一个**。
    """

    project_file: Path
    pilot_source: Path
    work_root: Path
    python_executable: str
    project_id: str = ""
    cycle_id: str = ""
    executor_name: str = ""
    approval_ttl_seconds: int = 900
    lease_ttl_seconds: int = 1800


@dataclass(frozen=True, slots=True)
class CycleContext:
    """造"脑子"时能看到的那些东西。

    **为什么要有它。** 这一轮的五步判断（观察 / 差距 / 发现 / 评分 / 更新）
    都需要知道"这是哪个仓库、这份目标、跑到哪儿了" —— 而这些**只有驱动器
    知道**：仓库是驱动器复制出来的，最新快照版本号是驱动器从数据库里查的。
    所以驱动器造一份上下文，配方拿它去造脑子。

    **它不含任何"这一轮想干什么"的东西。** 那是配方自己的事。
    """

    #: 项目定义 —— 从目标 YAML 里载进来的。
    project: Project
    #: 项目 id。
    project_id: str
    #: 这一轮用的仓库副本。
    repo: Path
    #: 测试报告的路径 —— Critic 的第一问看它。
    report_path: Path
    #: 工作根目录。
    work_root: Path
    #: 跑测试用的解释器。
    python_executable: str
    #: 上一份快照的版本号（没有就是 0）。
    latest_snapshot_version: int


@dataclass(frozen=True, slots=True)
class CycleBrain:
    """这一轮的"脑子" —— 循环 B 里那五个需要判断的步骤。

    **五个口对应图上的五个节点。** 观察、差距分析、发现候选、价值风险成本
    评估、轮后更新差距 —— 换脑子就是换这五个实现，其余的节点一个都不动。
    """

    observer: ProjectObserverPort
    analyzer: GapAnalyzerPort
    discoverer: CandidateDiscovererPort
    evaluator: CandidateEvaluatorPort
    gap_updater: GoalGapUpdaterPort


@dataclass(frozen=True, slots=True)
class CycleRecipe:
    """一轮的"脑子" —— **换脑子不换链路**。

    **为什么把它做成一个配方。** M1 与 M2 走的是同一张图、同一套闸门、同一套
    落库；不一样的只有"谁观察、谁发现、怎么评分"。把不一样的部分收进一个
    配方里，链路那部分就只有一份 —— 而链路只有一份，才谈得上"两轮可比"。

    :attr:`brain`: 造脑子的工厂。**它拿到上下文**（仓库、目标、快照版本），
        因为"发现候选"这一步要跑测试、要看仓库 —— 那些东西只有驱动器知道。
    :attr:`task_instruction`: 交给执行器的施工说明。**可以是字符串，也可以是
        一个按任务标题算出来的函数** —— M2 的说明取决于系统自己选中了哪条
        候选，写死一句就与那一轮对不上了。
    :attr:`task_key`: 任务标识（领域侧）。**可以是字符串，也可以是按候选 id
        算出来的函数** —— 一轮派生多个任务时，标识必须各不相同，否则路线图
        里会出现两个同名任务，而"任务标识"正是任务与决策、任务与执行结果
        之间那根线。
    :attr:`actuals`: "现在测到的关键结果"。**留空就退回目标 YAML 里的
        baseline** —— 那是目标的一部分，不是某一轮的判断。
    """

    #: 造脑子的工厂。
    brain: Callable[[CycleContext], CycleBrain]
    #: 执行器表 —— 名字 → 执行器。
    executors: Mapping[str, ExecutorPort]
    #: 用哪个执行器。**留空就取配置里那一个**，配置里也留空就报错。
    executor_name: str = ""
    #: 交给执行器的施工说明（字符串或按任务标题算出来的函数）。
    task_instruction: str | Callable[[str], str] = ""
    #: 施工步骤 —— 留空就用施工说明本身。
    task_steps: tuple[str, ...] = ()
    #: 任务标识（字符串或按候选 id 算出来的函数）。
    task_key: str | Callable[[str], str] = "T-001"
    #: 这一轮用的差距 id —— 差距分析与轮后更新都指着它。
    gap_id: str = "gap-001"
    #: "现在测到的关键结果" —— 留空就取目标 YAML 里的 baseline。
    actuals: Callable[[Project], Mapping[str, TargetValue]] | None = None
    #: 独立验证在 ``verification_run.verified_by`` 里留的名字。
    verifier_name: str = "independent-verifier"
    #: 合并审批的批准人 —— **谁批的**要留在库里。
    approver: str = "demo-approver"
    #: 批准时写的批语。
    merge_comment: str = "演示：人工放行这一次合并"
    #: 合入主分支时写的提交说明。
    merge_message: str = "合入本轮闭环的产出"
    #: 复制出来的仓库在 git 里署名用的邮箱与名字。
    repo_email: str = "ases@ases.local"
    repo_name: str = "ases"
    #: 周期 id 的前缀 —— 事后看数据库，"哪一轮是哪一轮"从 id 上就分得开。
    cycle_prefix: str = "cycle"
    #: 验收契约的模板。
    contract_template: ContractTemplate = field(default_factory=ContractTemplate)


@dataclass(slots=True)
class TaskLedger:
    """一个任务在这一轮里的账 —— **抄出来的，不重算的**。

    **一轮可能有多个任务。** M1 只有一条，M2 有好几条 —— 而"这一轮合了几次
    并、花了多少钱、改了几个文件"只有把每一条都摊开才说得清。
    """

    #: 任务行 id。
    task_id: str = ""
    #: 任务标题。
    title: str = ""
    #: 隔离工作区的路径。
    workspace_path: str = ""
    #: 干活的那条分支。
    branch: str = ""
    #: 改了几个文件。
    changed_files: int = 0
    #: 这一条花了多少钱。
    cost: Decimal = Decimal(0)
    #: 币种。
    currency: str = "USD"
    #: 独立验证的结论。
    verdict: str = ""
    #: 验证没过的理由。
    reasons: tuple[str, ...] = ()
    #: 逐项检查的结论 —— ``(检查项, 通过了没有)``。
    checks: tuple[tuple[str, bool], ...] = ()
    #: 验收契约判过没有。
    accepted: bool = False
    #: 合进主分支了没有。
    merged: bool = False
    #: 合入的提交号。
    merge_commit: str = ""
    #: 审批请求 id。
    approval_request_id: str = ""


@dataclass(frozen=True, slots=True)
class CycleResult:
    """一轮跑完之后的结论 —— **每一样都能对账**。

    :attr:`records: 落库的每一行是什么。键是表名，值是行 id。答"每一步的
        DB 记录可查"这个问题靠的就是它。
    """

    project_id: str
    cycle_id: str
    cycle_ref: str
    candidate_id: str
    decision_id: str
    task_key: str
    task_id: str
    approval_request_id: str
    verdict: VerificationVerdict
    reasons: tuple[str, ...]
    checks: tuple[tuple[str, bool], ...]
    accepted: bool
    merged: bool
    merge_commit: str
    workspace_path: str
    branch: str
    changed_files: int
    elapsed_seconds: Decimal
    cost: Decimal
    currency: str
    records: Mapping[str, str] = field(default_factory=dict)
    #: 这一轮的结局判定（``improved`` / ``no_change`` / ``regressed`` / ``stalled``）。
    cycle_outcome: str = ""
    #: 结局判定的理由 —— 结论要能被审。
    cycle_reason: str = ""
    #: 循环 B 走过的节点（按次序）。
    visited_nodes: tuple[str, ...] = ()
    #: Critic 九问的逐项结论 —— ``(检查项, 判定, 摘要)``。
    critic_outcomes: tuple[tuple[str, str, str], ...] = ()
    #: 这一轮**全部**候选 —— ``(候选 id, 标题, 置信度)``。
    candidates: tuple[tuple[str, str, str], ...] = ()
    #: 这一轮**全部**决策 —— ``(候选 id, 决策, 理由)``。
    decisions: tuple[tuple[str, str, str], ...] = ()
    #: 候选行 id —— 候选 id → 行 id。
    candidate_rows: Mapping[str, str] = field(default_factory=dict)
    #: 决策行 id —— 决策 id → 行 id。
    decision_rows: Mapping[str, str] = field(default_factory=dict)
    #: 任务行 id —— 任务标识 → 行 id。
    task_rows: Mapping[str, str] = field(default_factory=dict)
    #: 每个任务各自的账。
    task_ledgers: tuple[TaskLedger, ...] = ()
    #: 这一轮**之前**的目标达成度。
    attainment_before: Decimal | None = None
    #: 这一轮**之后**的目标达成度。
    attainment_after: Decimal | None = None
    #: 达成度差值 —— 后减前。**没有可比的前后值就是 ``None``**，而不是 0。
    attainment_delta: Decimal | None = None


def new_cycle_id(prefix: str = "cycle") -> str:
    """造一个这一轮专用的周期 id。

    **为什么不用一个写死的 id。** 同一个 ``cycle_id`` 开两次周期，仓储层
    会当场报错 —— 那条规则是对的：两条流程抢同一轮的时候，两边的候选与决策
    会混进同一个周期，而计数看起来仍然像个整数。演示与测试都要反复跑，
    所以每次跑要一个自己的周期 id。

    **id 里带着时间戳的前缀。** 事后看数据库，"哪一轮是哪一轮"从 id 上就
    分得开，不用再去查时间列。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------
# 端口
# --------------------------------------------------------------------------


class ContractBinder(Protocol):
    """把验收契约绑到任务行上（R-051）。

    **绑定必须发生在任务开工之前。** 状态机里 ``READY → RUNNING`` 有一道
    引擎级的闸：任务行上绑着**这个任务**的契约才许开工。绕开它，任务就能
    在没有验收标准的情况下跑完 —— 而那正是"验收通过"变成空话的情形。
    """

    async def bind(self, task_id: str) -> AcceptanceContract:
        """给这个任务绑一份验收契约，返回绑上去的那份。"""

    def is_bound(self, task_id: str) -> bool:
        """这个任务绑上契约了没有。"""


class MergeGranter(Protocol):
    """人在回路的那一步 —— 合并要审批时，由它替人做决定。

    **它不是"自动放行"。** 实现它的那一侧必须真的走一遍审批门
    （发起请求 → 记录批准人 → 落库），否则审批就是被绕过的，而绕过的审批
    在账上看不出来。

    **它拿的是请求 id，不是任务 id。** 人批的是**某一次具体的请求** ——
    请求里带着那一次改动的 diff 摘要。给任务 id 就等于"批这个任务的所有
    合并"，那不是审批，那是授权。
    """

    async def grant(self, *, request_id: str) -> str:
        """批下这一次合并，返回批准人。"""


class TaskLookupPort(Protocol):
    """任务表查询口 —— 给 Critic 的第三问用。"""

    async def find_similar(self, text: str, *, limit: int = 20) -> tuple[TaskSummary, ...]: ...


# --------------------------------------------------------------------------
# 这一轮的可变账本
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RunLedger:
    """这一轮实际发生的事 —— **抄出来的，不重算的**。

    **成本是从执行结果里抄的，不是从基线表里查的。** T2 要拿它跟基线比，
    而"比的那个数"必须是这一轮真的花掉的那个数 —— 从别处抄来的数会让
    "落在基线 2 倍以内"这句话变成空话。

    **验收结论也抄在这里。** 循环 B 的状态里没有循环 A 的验收报告（那条链
    上不传），所以结论由验证器自己交上来 —— 抄一份在这里，报告里才有话说。
    """

    #: 这一轮**总共**花了多少钱 —— 各个任务成本的和。
    cost: Decimal = Decimal(0)
    currency: str = "USD"
    #: 这一轮**总共**改了几个文件 —— 各个任务改动数的和。
    changed_files: int = 0
    commit: str = ""
    branch: str = ""
    workspace_path: str = ""
    approval_request_id: str = ""
    merged: bool = False
    merge_commit: str = ""
    verification: VerificationReport | None = None
    #: 这一次独立验证花了多少毫秒 —— **抄给 ``verification_run.duration_ms``**。
    verification_duration_ms: int | None = None
    #: 每个任务各自的账。
    tasks: list[TaskLedger] = field(default_factory=list)

    def entry(self, task_id: str) -> TaskLedger:
        """取这个任务在账本里的那一条 —— **没有就建一条**。

        **为什么按任务 id 记，而不是各抄各的。** 一轮可能有多个任务
        （M2 有十几条），而"这一轮花了多少钱"是**它们加起来**的钱；把
        成本抄到一个共享的字段上，最后一个任务会盖掉前面所有的 —— 于是
        报表上写着"这一轮花了 0.6 美元"，而实际上是 5.4 美元。
        """
        for item in self.tasks:
            if item.task_id == task_id:
                return item
        item = TaskLedger(task_id=task_id)
        self.tasks.append(item)
        return item


@dataclass(slots=True)
class CycleRefHolder:
    """周期行 id 的传递盒子。

    **为什么需要它。** 周期行 id 是 ``open_cycle`` 的返回值 —— 而验证归档
    发生在周期开出来**之后**。可验证器是周期开出来之前就造好的，它拿不到
    那个 id，除非有人把它递过去。

    **这里递的是真值，不是猜的。** 验证记录里那个 ``cycle_id`` 列是指向
    周期行的外键 —— 填错的话，插入会被外键挡下来；填成周期 id（``"cycle-
    m1-..."`` 那种字符串）连类型都不对。
    """

    value: str = ""


# --------------------------------------------------------------------------
# 落库辅助
# --------------------------------------------------------------------------


class BindingJournal(EvolutionJournalPort):
    """在任务落库之后**立刻**把验收契约绑上去。

    **为什么包一层而不是在调用方绑。** 任务是循环 B 的
    ``decompose_into_tasks`` 那一步落下去的，而契约的 ``task_id`` 必须等于
    任务行 id —— 那个 id 在落库那一刻才确定。所以绑定只能发生在
    ``land_tasks`` 之后：包一层是唯一能保证"绑了"与"落库了"挨在一起的做法。

    **绑完立刻提交 —— 这一步是必须的，不是洁癖。** 任务行与周期行在别的
    事务里被外键引用：``verification_run.task_id`` 指向 ``task.id``，
    ``verification_run.cycle_id`` 指向 ``evolution_cycle.id``，而产物登记处
    （:class:`~infrastructure.database.artifact.PostgresArtifactRegistry`）
    **自己开事务**写这两张表。外键检查要等被引用的行**已提交**才过得去 ——
    本会话还攥着未提交的行时，另一笔事务插 ``verification_run`` 就会一直
    等本会话提交，而本会话正等着那一笔的返回：**死锁**。

    提交在这里发生，还因为任务行提交之后，后续对它的 ``UPDATE`` 取的是
    ``FOR NO KEY UPDATE`` 锁 —— 它与外键检查要的 ``FOR KEY SHARE`` **不
    冲突**，所以验证那一步在另一个事务里插 ``verification_run`` 不会被
    本会话的行锁挡住。

    **它不改别的东西。** 别的写入口径全部转给它里面的那个。
    """

    __slots__ = ("_binder", "_cycle_refs", "_inner", "_session")

    def __init__(
        self,
        inner: EvolutionJournal,
        binder: ContractBinder,
        session: AsyncSession,
        cycle_refs: CycleRefHolder,
    ) -> None:
        self._inner = inner
        self._binder = binder
        self._session = session
        self._cycle_refs = cycle_refs

    async def open_cycle(self, *, project_id: str, cycle_id: str, trigger: str) -> str:
        cycle_ref = await self._inner.open_cycle(
            project_id=project_id, cycle_id=cycle_id, trigger=trigger
        )
        # **周期行 id 立刻交出去。** 验证归档要用它 —— 而验证发生在后面。
        self._cycle_refs.value = cycle_ref
        return cycle_ref

    async def record_candidates(
        self, cycle_ref: str, candidates: Sequence[EvolutionCandidate]
    ) -> Mapping[str, str]:
        return await self._inner.record_candidates(cycle_ref, candidates)

    async def record_decisions(
        self,
        cycle_ref: str,
        decisions: DecisionRound,
        candidate_rows: Mapping[str, str],
    ) -> Mapping[str, str]:
        return await self._inner.record_decisions(cycle_ref, decisions, candidate_rows)

    async def land_tasks(
        self, roadmap: EvolutionRoadmap, decision_rows: Mapping[str, str]
    ) -> Mapping[str, str]:
        rows = await self._inner.land_tasks(roadmap, decision_rows)
        for task_id in rows.values():
            await self._binder.bind(task_id)
        # **提交在这里，不在别处。** 见类说明：任务行与周期行必须已提交，
        # 外键才过得去。
        await self._session.commit()
        return rows

    async def record_snapshot(
        self,
        *,
        project_id: str,
        version: int,
        commit: str,
        branch: str,
        metrics: Mapping[str, float],
    ) -> str:
        return await self._inner.record_snapshot(
            project_id=project_id,
            version=version,
            commit=commit,
            branch=branch,
            metrics=metrics,
        )

    async def close_cycle(
        self,
        cycle_ref: str,
        *,
        status: CycleStatus,
        attainment_before: Decimal | None,
        attainment_after: Decimal | None,
        evidence_refs: Sequence[str],
    ) -> None:
        await self._inner.close_cycle(
            cycle_ref,
            status=status,
            attainment_before=attainment_before,
            attainment_after=attainment_after,
            evidence_refs=evidence_refs,
        )


class DatabaseContractBinder:
    """把验收契约绑到任务行上 —— 数据库实现。

    **契约按任务 id 现造。** 契约里写死了 ``task_id``，而任务行 id 是落库
    时才有的 —— 所以先拿模板，再按任务 id 造出真正的那一份。

    **"绑了没有"自己记一份。** 循环 A 要用**同步**的 ``contract_bound_of``
    回答"这个任务绑契约了没有"，而落库是异步的 —— 记一份内存里的集合，
    那个同步回调答的就是真话，不是猜的。
    """

    __slots__ = ("_bound", "_repository", "_template")

    def __init__(self, repository: TaskRepository, template: ContractTemplate) -> None:
        self._repository = repository
        self._template = template
        self._bound: set[str] = set()

    async def bind(self, task_id: str) -> AcceptanceContract:
        contract = self._template.for_task(task_id)
        await self._repository.bind_acceptance_contract(uuid.UUID(task_id), contract.to_document())
        self._bound.add(task_id)
        return contract

    def is_bound(self, task_id: str) -> bool:
        return task_id in self._bound


@dataclass(frozen=True, slots=True)
class Planner:
    """生成演进计划 —— **真审一遍**，审过了才放行。

    **放行凭据来自 Critic 的报告。** 九项检查里只要有一项构成否决，这条
    决策就拿不到凭据，任务也就派生不出来 —— 那正是"候选 ≠ 任务"那句话
    在代码里的样子。

    **任务标识按候选给。** 一个候选派生一个任务时，写死一个标识没问题；
    一轮派生九个任务时，九个任务共用一个标识，路线图里就会有两个同名任务
    —— 而"任务标识"正是任务与决策、任务与执行结果之间那根线（:func:
    core.evolution.evolution_loop.evaluate_outcome` 靠它对账）。所以这里
    收的是"按候选 id 算出标识"的函数。
    """

    critic: Critic
    task_key: str | Callable[[str], str] = "T-001"
    reports: list[CriticReport] = field(default_factory=list)

    async def plan(self, state: LoopBState) -> EvolutionPlan:
        decisions = state.decisions
        if decisions is None:
            return EvolutionPlan()

        drafts: list[TaskDraft] = []
        approvals: list[DecisionApproval] = []
        for decision in decisions.decisions:
            if decision.decision is not DecisionState.RECORD:
                continue
            candidate = candidate_of(state, decision.candidate_id)
            if candidate is None:
                continue
            report = await self.critic.review(candidate)
            self.reports.append(report)
            approvals.append(approval_for(decision, report))
            drafts.append(
                TaskDraft(
                    key=task_key_for(self.task_key, candidate.id),
                    decision_id=decision.id,
                    task_type=TaskType.BUGFIX,
                    title=candidate.title,
                    description=candidate.hypothesis,
                    rationale=decision.rationale,
                )
            )
        return EvolutionPlan(drafts=tuple(drafts), approvals=tuple(approvals))


@dataclass(frozen=True, slots=True)
class Scheduler:
    """资源调度 —— 一个任务、一波、一个执行器。

    **"谁来做"要落库，不能只活在内存里。** 调度结果（谁、哪一波）记在排期
    表里，而排期表只在编排层的状态里 —— 事后查一条任务，光看任务行是**不知道
    它被谁做了**的。会话行上确实记着执行器，可会话与任务是两件事，靠会话反推
    任务是猜。所以这里顺手把执行器写进任务行（:meth:`TaskRepository.
    assign_executor`）。

    **任务行这时已经在了。** 拆解那一步（``decompose_into_tasks``）先落库，
    调度这一步才跑 —— 所以这里拿得到任务行 id。
    """

    executor_name: str
    repository: TaskRepository
    session: AsyncSession

    async def schedule(self, state: LoopBState) -> ResourceSchedule:
        roadmap = state.roadmap
        if roadmap is None:
            return ResourceSchedule()
        assignments = tuple(
            ResourceAssignment(
                task_key=planned.key,
                wave=1,
                executor=self.executor_name,
                budget=Decimal("1"),
            )
            for planned in roadmap.tasks
        )
        await self._stamp_the_executors(state.task_rows)
        return ResourceSchedule(assignments=assignments, budget=Decimal("1"))

    async def _stamp_the_executors(self, task_rows: Mapping[str, str]) -> None:
        """把执行器写进任务行。

        **提交在这里。** 任务行已提交过一次（``land_tasks`` 之后），这一句是
        新事务里的第二次写 —— 不提交的话，这一笔会跟着主流程最后那次提交
        一起落库，而那一次可能根本不来（流程中途出错）。
        """
        for task_id in task_rows.values():
            await self.repository.assign_executor(uuid.UUID(task_id), self.executor_name)
        await self.session.commit()


# --------------------------------------------------------------------------
# 循环 A 的各步
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskPlanner:
    """任务规划 —— 施工说明按任务标题给。

    **说明是给执行器看的。** 这一版的执行器按补丁走，不读说明 —— 但说明
    仍然要写：它是"这一轮要做什么"的唯一人类可读记录，而它同时也是验收
    重点的来源。

    **说明跟着任务走。** 一轮里派生九个任务时，写死一句说明就与其中八个
    任务对不上 —— 而"说明与任务不符"这件事，事后没人看得出来。所以说明
    可以是一个按任务标题算出来的函数。
    """

    instruction: str | Callable[[str], str] = ""
    steps: tuple[str, ...] = ()

    async def plan(self, state: LoopAState) -> ExecutionPlan:
        instruction = (
            self.instruction(state.title) if callable(self.instruction) else self.instruction
        )
        return ExecutionPlan(
            task_id=state.task_id,
            instruction=instruction,
            task_type=TaskType.BUGFIX,
            steps=self.steps or (instruction,),
            timeout_seconds=600,
        )


@dataclass(frozen=True, slots=True)
class Selector:
    """选执行器 —— 只有一个候选，选它。"""

    executor_name: str

    async def select(self, state: LoopAState, candidates: Sequence[str]) -> str:
        del state  # 只有一个执行器，选择不取决于任务状态。
        if self.executor_name not in candidates:
            raise LookupError(f"执行器 {self.executor_name!r} 不在候选表里：{list(candidates)}")
        return self.executor_name


@dataclass(frozen=True, slots=True)
class Workspace:
    """开隔离工作区 —— **真的 git worktree**。

    **工作区在仓库之外。** 仓库里的 worktree 会被 git 当成未跟踪内容，主
    仓库的 ``git status`` 就永远不干净 —— 工作空间管理器自己也拒这种布局。

    **开完顺手把分支与工作区记进任务行。** 这两栏不记，事后查任务就只剩
    "完成了"，没人说得清它的改动落在哪条分支上 —— 而分支正是回滚与追责时
    要找的那条线。
    """

    manager: WorkspaceManager
    ledger: RunLedger
    repository: TaskRepository
    session: AsyncSession

    async def create(self, state: LoopAState) -> WorkspaceInfo:
        workspace = self.manager.acquire(state.task_id, holder=state.agent_id)
        self.ledger.workspace_path = str(workspace.path)
        self.ledger.branch = workspace.branch
        entry = self.ledger.entry(state.task_id)
        entry.workspace_path = str(workspace.path)
        entry.branch = workspace.branch
        entry.title = state.title
        await self.repository.assign_workspace(
            uuid.UUID(state.task_id),
            branch=workspace.branch,
            workspace_id=workspace.workspace_id,
        )
        await self.session.commit()
        return WorkspaceInfo(
            workspace_id=workspace.workspace_id,
            path=str(workspace.path),
            branch=workspace.branch,
        )


@dataclass(frozen=True, slots=True)
class Collector:
    """收集改动 —— 从执行结果里取，不自己看 git。

    **顺便把成本抄进账本。** 成本是执行器报的那个数（换算已在适配器那边
    做完），这里只抄不改 —— T2 拿它跟基线比，靠的就是"抄的那一份"。
    """

    ledger: RunLedger

    async def collect(self, state: LoopAState, result: ExecutionResult) -> CollectedArtifacts:
        artifacts = result.artifacts
        self.ledger.cost += result.execution.cost
        self.ledger.currency = result.execution.currency
        self.ledger.changed_files += artifacts.changed_files
        self.ledger.commit = artifacts.commit or ""
        if artifacts.branch:
            self.ledger.branch = artifacts.branch
        entry = self.ledger.entry(state.task_id)
        entry.cost = result.execution.cost
        entry.currency = result.execution.currency
        entry.changed_files = artifacts.changed_files
        entry.branch = artifacts.branch or ""
        entry.title = state.title
        return CollectedArtifacts(
            branch=artifacts.branch or "",
            commit=artifacts.commit,
            changed_files=artifacts.changed_files,
            diff_ref=artifacts.commit or "",
        )


@dataclass(frozen=True, slots=True)
class Verifier:
    """独立验证 —— **真的起一个进程跑真的 pytest**。

    **它拿不到执行器的自我报告。** 验证请求里只有任务、工作区、契约与
    计划 —— 没有 ``ExecutionResult``。这是 R-049 的硬边界：验证引擎独立于
    执行器，靠的是"那个字段压根不存在"，不是"大家记得别用它"。

    **产物同时归档。** 验证结论、测试报告与执行轨迹一起登记进产物表 ——
    事后能查到"当初验的是什么、验出了什么"。

    **工作区从状态里取，不从构造时取。** 工作区是循环 A 那一轮开出来的，
    构造验证器的时候它还不存在。
    """

    engine: VerificationEngine
    registry: PostgresArtifactRegistry
    template: ContractTemplate
    python_executable: str
    cycle_refs: CycleRefHolder
    ledger: RunLedger
    #: 独立验证在 ``verification_run.verified_by`` 里留的名字。
    verified_by: str = "independent-verifier"
    runs: list[str] = field(default_factory=list)

    async def verify(self, state: LoopAState) -> VerificationReport:
        workspace = state.workspace
        if workspace is None:
            raise RuntimeError("没有隔离工作区就无从验证 —— 工作区是验证进程跑起来的地方")
        contract = self.template.for_task(state.task_id)
        request = VerificationRequest(
            task_id=state.task_id,
            workspace=Path(workspace.path),
            contract=contract,
            plan=verification_plan(self.python_executable),
        )
        started = time.monotonic()
        run = await self.engine.verify(request)
        self.ledger.verification_duration_ms = int((time.monotonic() - started) * 1000)
        await self._archive(state, run)
        report = run.to_report()
        self.ledger.verification = report
        entry = self.ledger.entry(state.task_id)
        entry.verdict = report.verdict.value
        entry.reasons = report.reasons
        entry.checks = report.checks
        entry.title = state.title
        return report

    async def _archive(self, state: LoopAState, run: VerificationRun) -> None:
        """把这次验收与它的产物登记进产物表。

        **耗时记的是"这一次验收花了多久"。** 验证引擎自己不量时间（它量的是
        每项检查的结论，不是整轮的时长），而这一栏空着的话，事后没人说得清
        "这次验收是快了还是慢了" —— 空着的耗时栏与"没量"长得一模一样。
        """
        run_id = str(uuid.uuid4())
        trace = "\n".join(run.traces)
        record = VerificationRunRecord(
            run_id=run_id,
            task_id=state.task_id,
            checks=tuple(
                {"check": check.value, "outcome": result.outcome.value}
                for check, result in run.checks.items()
            ),
            result=run.to_report().verdict.value,
            verified_by=self.verified_by,
            duration_ms=self.ledger.verification_duration_ms,
            cycle_id=self.cycle_refs.value or None,
        )
        artifacts = (
            ArtifactRecord(
                artifact_id=str(uuid.uuid4()),
                task_id=state.task_id,
                verification_run_id=run_id,
                uri=f"memory://verification/{run_id}/execution-trace",
                hash=sha256_of(trace),
                type=ArtifactKind.EXECUTION_TRACE,
                size_bytes=len(trace.encode("utf-8")),
                project_id=state.project_id,
            ),
        )
        await self.registry.record_archive(record, artifacts)
        self.runs.append(run_id)


@dataclass(frozen=True, slots=True)
class Diagnoser:
    """失败诊断 —— 这一版只报"不可自动修"。

    **不可自动修是这里最诚实的答案。** 执行器是按固定补丁走的：补丁对不
    上就是仓库变了，再跑一遍还是对不上 —— 那不是"再试一次能好"的情形。
    """

    async def diagnose(self, state: LoopAState) -> FailureDiagnosis:
        verification = state.verification
        reasons = "; ".join(verification.reasons) if verification else "没有验证结论"
        return FailureDiagnosis(
            reason=reasons or "独立验证没有通过",
            category=FailureCategory.UNREPAIRABLE,
            detail="补丁对不上当前工作区 —— 需要人来看",
        )


@dataclass(frozen=True, slots=True)
class MergeGate:
    """合并闸门 —— **先请人批，再问策略，最后才 merge**。

    **顺序不能换。** 策略对 ``merge_to_main`` 的判定是"需要审批"，而审批
    记录在审批门那一张表里 —— **先请求、先批，再拿着批文问策略**，策略才会
    给出"放行"。顺序反过来（先问策略再请人批）的话，问策略那一刻还没有
    任何审批记录，判定停在"需要审批"，于是**这一轮永远合不上** —— 而且
    报出来的是一句"需要审批"，看不出是顺序错了。

    **审批请求必须带 diff 摘要。** 审批门自己会拒空摘要 —— 人批的是他看到
    的那份改动，没有摘要的批准是对一件不知道是什么的事点头。

    **diff 摘要比的是 ``main...分支``。** 比 ``main...HEAD`` 是错的：合并
    闸门跑在主仓库里，而主仓库的 HEAD **就是** main，那条 diff 恒为空 ——
    审批门会以"必须带 diff 摘要"把它拒掉。
    """

    repo: Path
    policy: PolicyManager
    gate: ApprovalGate
    granter: MergeGranter
    ledger: RunLedger
    #: 合入主分支时写的提交说明。
    merge_message: str = "合入本轮闭环的产出"

    async def merge(self, state: LoopAState) -> MergeOutcome:
        workspace = state.workspace
        if workspace is None:
            return MergeOutcome(merged=False, reason="没有隔离工作区就没有可合的分支")
        context = TaskContext(
            agent_id=state.agent_id,
            task_id=state.task_id,
            project_id=state.project_id,
            workspace_id=workspace.workspace_id,
        )
        request = await self.gate.request(
            task_id=state.task_id,
            operation=Operation.MERGE_TO_MAIN,
            diff_summary=diff_summary(self.repo, workspace.branch),
            evidence_ref=state.task_id,
            requested_by=state.agent_id,
        )
        self.ledger.approval_request_id = request.request_id
        entry = self.ledger.entry(state.task_id)
        entry.approval_request_id = request.request_id
        await self.granter.grant(request_id=request.request_id)
        decision = await self.policy.authorize(Operation.MERGE_TO_MAIN, context, approval=self.gate)
        if not decision.is_allowed:
            return MergeOutcome(merged=False, reason=decision.reason)
        run_git(
            self.repo,
            "merge",
            "--no-ff",
            "-m",
            self.merge_message,
            workspace.branch,
        )
        # **合完之后问一次 HEAD。** ``git merge`` 的输出是给人看的（"用了什么
        # 策略"、"改了哪些文件"），不是提交号 —— 把它当成提交号存进"合入的
        # 是哪一次提交"那一栏，存进去的就是一段散文。
        merge_commit = run_git(self.repo, "rev-parse", "HEAD").strip()
        self.ledger.merged = True
        self.ledger.merge_commit = merge_commit
        entry = self.ledger.entry(state.task_id)
        entry.merged = True
        entry.merge_commit = merge_commit
        return MergeOutcome(merged=True, commit=merge_commit, reason="已合入主分支")


@dataclass(frozen=True, slots=True)
class HumanMergeGranter:
    """人在回路的那一步 —— 由"人"批下这一次合并。

    **它真的走审批门。** 批准人、批准时间与那条 diff 摘要一起落库 ——
    事后查得到"谁在什么时候批了哪一份改动"。绕过审批门直接放行的话，
    审批这一栏就永远是空的，而空着的审批栏看起来跟"批过了"一模一样。
    """

    gate: ApprovalGate
    approver: str = "demo-approver"
    comment: str = "演示：人工放行这一次合并"

    async def grant(self, *, request_id: str) -> str:
        request = await self.gate.approve(
            request_id=request_id,
            approver=self.approver,
            comment=self.comment,
        )
        return request.decided_by or self.approver


@dataclass(frozen=True, slots=True)
class ProjectState:
    """回写项目状态 —— **把任务行推到完成**。

    **它推的是数据库里的任务状态机。** 任务行停在 ``MERGED`` 而不是
    ``COMPLETED`` 上的话，看板上就永远看不到"做完了"。

    **只在真的停在 ``MERGED`` 上才推。** 这一步在合并失败那条路上也会走
    （挂起的任务同样要回写），而挂起时任务行停在 ``BLOCKED`` —— 从那儿
    推进 ``COMPLETED`` 是非法迁移，状态机会当场抛错。
    """

    repository: TaskRepository

    async def update(self, state: LoopAState) -> None:
        task_id = uuid.UUID(state.task_id)
        task = await self.repository.get(task_id)
        if TaskState(task.status) is not TaskState.MERGED:
            return
        await self.repository.transition(task_id, TaskAction.COMPLETE)


# --------------------------------------------------------------------------
# 任务行与执行轨迹的镜像
# --------------------------------------------------------------------------

#: 任务行从落库到完成要走的主路径 —— 每一项是「从哪个状态 · 用哪个动作」。
#:
#: **落库那一刻任务行停在 ``APPROVED``**（那是
#: :meth:`TaskRepository.create_from_decision` 给的初态），而循环 A 是从
#: ``READY`` 起步的。这条路径把两者的差距补上：中间那几步**不是**补出来
#: 的假动作，而是状态机本来就要求的迁移 —— ``READY → RUNNING`` 那道
#: 验收契约闸门就在这条路径上，绕过去的话任务就是"没有验收标准也开工"。
MAIN_PATH: Final[tuple[tuple[TaskState, TaskAction], ...]] = (
    (TaskState.APPROVED, TaskAction.PLAN),
    (TaskState.PLANNED, TaskAction.READY),
    (TaskState.READY, TaskAction.START),
    (TaskState.RUNNING, TaskAction.VERIFY),
    (TaskState.VERIFYING, TaskAction.PASS),
    (TaskState.PASSED, TaskAction.INTEGRATE),
    (TaskState.INTEGRATING, TaskAction.MERGE),
    (TaskState.MERGED, TaskAction.COMPLETE),
)


def actions_to_reach(current: TaskState, target: TaskState) -> tuple[TaskAction, ...]:
    """从 ``current`` 走到 ``target`` 要走的动作。

    **走到哪一步算哪一步，不猜。** 主路径是一条直线，方向反了（要往回走）
    或者起点不在路径上，都直接报错 —— 那种情况说明镜像与编排层对不上了，
    而"对不上"这件事必须喊出来，不能悄悄停在某个状态上。
    """
    if target is current:
        return ()
    if target is TaskState.BLOCKED:
        # 挂起只有一条路：从当前状态直接 BLOCK。
        return (TaskAction.BLOCK,)
    states = (*(state for state, _ in MAIN_PATH), TaskState.COMPLETED)
    actions = tuple(action for _, action in MAIN_PATH)
    if current not in states:
        raise ValueError(f"任务行停在 {current.value} —— 不知道从那儿怎么走到 {target.value}")
    if target not in states:
        raise ValueError(f"目标状态 {target.value} 不在主路径上")
    start = states.index(current)
    end = states.index(target)
    if end < start:
        raise ValueError(f"任务行不能从 {current.value} 往回走到 {target.value}")
    return actions[start:end]


class MirroringSessionJournal:
    """把执行轨迹里报的任务状态搬到**数据库的任务状态机**上。

    **为什么不直接读编排层的内存状态。** 编排层的状态是这一轮的内存视图，
    数据库的任务行才是**别人查得到的那一份**。T1 的验收口径是"每一步的
    DB 记录可查" —— 查的是数据库里的任务行。

    **报出来的状态是唯一的信息源。** 每个节点写的事件载荷里都带着
    ``task_states``（见 :func:`orchestration.loop_a._record`），那串状态就是
    这一步"经过了哪些任务状态"。镜像照着它走，**不自己判断**该走哪一步 ——
    自己判断就是第二份状态机，两份迟早对不上。
    """

    __slots__ = ("_inner", "_repository", "states")

    def __init__(self, inner: DatabaseExecutionJournal, repository: TaskRepository) -> None:
        self._inner = inner
        self._repository = repository
        #: 这一轮走过的任务状态序列 —— 事后能对着轨迹复核。
        self.states: list[TaskState] = []

    async def open_session(self, record: SessionRecord) -> str:
        return await self._inner.open_session(record)

    async def record_event(self, event: EventRecord) -> None:
        await self._inner.record_event(event)
        for target in reported_states(event):
            await self._advance(event.task_id, target)
            self.states.append(target)

    async def _advance(self, task_id: str, target: TaskState) -> None:
        """把任务行推到 ``target``。"""
        identifier = uuid.UUID(task_id)
        task = await self._repository.get(identifier)
        current = TaskState(task.status)
        for action in actions_to_reach(current, target):
            await self._repository.transition(identifier, action)


def reported_states(event: EventRecord) -> tuple[TaskState, ...]:
    """从事件载荷里读出这一步经过的任务状态。"""
    raw = event.payload.get("task_states")
    if raw is None or isinstance(raw, str) or not isinstance(raw, Sequence):
        return ()
    states: list[TaskState] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        states.append(TaskState(item))
    return tuple(states)


# --------------------------------------------------------------------------
# 计划与工具
# --------------------------------------------------------------------------


def verification_plan(python_executable: str) -> VerificationPlan:
    """这一轮的验证计划。

    **检查项与命令都写死在这儿。** 命令**不开 shell** —— 参数是外部输入，
    ``shell=True`` 等于把执行权交给一份配置文件。

    **lint 的规则集是显式给的**（``--select E4,E7,E9,F``）。不写的话，
    ruff 的默认规则集随版本变，而"今天 lint 红了"与"代码变坏了"是两件事
    —— 那条红灯会让人去查代码，而问题在工具的默认值上。

    **禁止项扫描用一段内联 Python。** 扫描器的约定是"退出码 0 表示干净"，
    而 ``grep`` 的约定正好相反（没匹配返回 1）—— 直接用它会把"干净"判成
    "命中"。所以这里用一段自己算退出码的代码。
    """
    secret_scan = (
        "import pathlib,re,sys\n"
        "pats=[re.compile(r'AKIA[0-9A-Z]{16}'),"
        "re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')]\n"
        "hits=[]\n"
        "for p in pathlib.Path('.').rglob('*.py'):\n"
        "    try:\n"
        "        t=p.read_text(encoding='utf-8',errors='ignore')\n"
        "    except OSError:\n"
        "        continue\n"
        "    if any(q.search(t) for q in pats):\n"
        "        hits.append(str(p))\n"
        "print('\\n'.join(hits))\n"
        "sys.exit(1 if hits else 0)\n"
    )
    return VerificationPlan(
        checks=(
            CheckSpec(
                check=RequiredCheck.UNIT_TESTS_PASS,
                command=(python_executable, "-m", "pytest", "tests", "-q"),
                timeout_seconds=300,
                produces=(ArtifactKind.TEST_REPORT,),
            ),
            CheckSpec(
                check=RequiredCheck.LINT_PASS,
                command=(python_executable, "-m", "ruff", "check", "--select", "E4,E7,E9,F", "."),
                timeout_seconds=120,
                produces=(),
            ),
        ),
        scans=(
            ScanSpec(
                rule=ForbiddenRule.SECRETS_IN_DIFF,
                command=(python_executable, "-c", secret_scan),
                timeout_seconds=60,
                produces=(ArtifactKind.GIT_DIFF,),
            ),
        ),
    )


def build_critic(
    *,
    project: Project,
    repo: Path,
    report_path: Path,
    lookup: TaskLookupPort | None,
    actuals: Mapping[str, TargetValue] | None = None,
) -> Critic:
    """把 Critic 的事实核验器配齐。

    **配哪些工具是有讲究的。** 没配的工具 → "未核实" → 不否决，但也不构成
    证据。这里配上仓库搜索、Git 历史、项目状态、测试报告与任务表 —— 它们
    覆盖了 Critic 九问里那五问**能否否**的检查所需的事实。

    **``actuals`` 是"现在测到的关键结果"。** 不给它的话，"值不值得现在做"
    那一问是**未核实**（不是"不值得"，是"不知道"）；给了它，那一问就能给出
    明确结论 —— 试点项目的四条关键结果都还没达成，所以答案是"值得做"。
    """
    checker = FactChecker()
    checker.register(RepositorySearchTool(root=repo))
    checker.register(GitHistoryTool(root=repo))
    checker.register(ProjectStatusTool(goal=project.goal, actuals=actuals))
    if report_path.exists():
        checker.register(TestReportTool(path=report_path))
    if lookup is not None:
        checker.register(TaskTableTool(lookup=lookup))
    return Critic(checker)


def pilot_actuals(project: Project) -> Mapping[str, TargetValue]:
    """试点项目**当前**的关键结果实测值。

    **取自 ``project.yaml`` 里各关键结果的 ``baseline``。** 那一列写的就是
    "现在测到了多少" —— 拿它当实测值不是编造：M1 跑的就是这份基线代码，
    缺陷还没修，指标与基线一致。

    **实测值按关键结果 id 给。** 目标达成度是**按 id** 对的（见
    :meth:`core.project.stop_conditions.GoalAttainmentCalculator.evaluate`），
    给错了键就等于"没测" —— 而"没测"会让"值不值得现在做"退回未核实。
    """
    actuals: dict[str, TargetValue] = {}
    for key_result in project.goal.key_results:
        baseline = key_result.baseline
        if baseline is None:
            continue
        if key_result.metric_type is MetricType.BOOLEAN:
            actuals[key_result.id] = TargetValue(kind=ValueKind.BOOLEAN, flag=bool(baseline))
        else:
            actuals[key_result.id] = TargetValue(kind=ValueKind.NUMBER, number=baseline)
    return actuals


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def diff_summary(repo: Path, branch: str) -> str:
    """改动摘要 —— **审批请求必须带它**。

    **比的是 ``main...分支``。** 合并闸门跑在主仓库里，主仓库的 HEAD 就是
    main —— 比 ``main...HEAD`` 得到的永远是空摘要，而审批门会以"必须带
    diff 摘要"把请求拒掉。
    """
    if not branch:
        return "（没有分支）"
    completed = subprocess.run(
        ["git", "diff", "--stat", f"{MAIN_BRANCH}...{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return "（没有改动）"
    return completed.stdout.strip() or "（没有改动）"


def sha256_of(text: str) -> str:
    """内容的摘要 —— 产物以 hash 寻址（R-024）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def materialize_pilot_repo(
    source: Path,
    destination: Path,
    *,
    email: str = "ases@ases.local",
    name: str = "ases",
) -> Path:
    """把试点仓库的源码复制成**一份独立的 git 仓库**。

    **为什么要复制而不是直接用。** ``samples/pilot_repo`` 是本仓库的一个
    子目录，不是独立的 git 仓库 —— 工作空间管理器要的是"一个能开 worktree
    的仓库"。复制一份出来，主目录一个字都不动，而这一轮的所有改动都发生
    在副本里。

    **复制时跳过缓存与产物。** ``.pytest_cache`` / ``__pycache__`` /
    ``.coverage`` 是跑测试的残留，带进新仓库会让"这一轮改了什么"变得 noisy。
    """
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        if item.name in {".pytest_cache", "__pycache__", ".coverage", ".git"}:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".coverage"),
            )
        else:
            shutil.copy2(item, target)
    run_git(destination, "init", "-b", MAIN_BRANCH)
    run_git(destination, "config", "user.email", email)
    run_git(destination, "config", "user.name", name)
    run_git(destination, "add", "-A")
    run_git(destination, "commit", "-m", "导入试点仓库基线")
    return destination


def write_test_report(repo: Path, python_executable: str, report_path: Path) -> Path:
    """跑一遍试点仓库的**复现套件**，把结果落成一份测试报告。

    **为什么要有这份报告。** Critic 的第一问（问题是否真实存在）看的是
    "测试报告里有没有一条提到这件事的失败用例"。这份报告是真的跑出来的 ——
    不是手写的 JSON。

    **它跑的是复现套件，不是自有套件。** 复现套件里的用例全是 xfail；
    而"缺陷真的存在"这件事，靠的正是那些用例**真的失败**。

    **必须加 ``--runxfail``。** 不加的话，pytest 把 xfail 的用例记成
    "预期中的失败"（``skipped``），而测试报告工具**只看失败的用例** ——
    报告里一条失败都没有，"缺陷存在"这件事就永远证明不了。

    **报告用 ``--junit-xml`` 拿，不用解析终端输出。** ``pytest -q`` 不打每条
    用例的 node id（那是 ``-v`` 才有的），靠正则去捞输出捞出来的是一堆
    摘要行 —— 报告里的 node id 得是**能定位到那一条用例**的字符串。

    **报告写在仓库外面。** 报告是这一轮的中间产物，不是被验收项目的代码 ——
    放进仓库里的话，它会跟着工作树一起进 diff，而"这一轮改了什么"里就会
    多出一样与改动无关的东西。
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path = report_path.with_suffix(".xml")
    subprocess.run(
        [
            python_executable,
            "-m",
            "pytest",
            "known_issues",
            "--runxfail",
            f"--junit-xml={junit_path}",
            "-q",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    tests = parse_junit(junit_path)
    report_path.write_text(
        json.dumps({"tests": tests}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report_path


def parse_junit(path: Path) -> list[dict[str, str]]:
    """把 JUnit XML 读成测试报告工具认得的形状。

    **node id 由 ``classname`` 还原。** pytest 的 JUnit XML 里
    ``classname`` 是模块的点分路径（``known_issues.test_known_issues``），
    没有单独的 ``file`` 属性 —— 所以模块名（最后一段）还原成 ``.py``，
    前面几段还原成目录。

    **"跳过"与"失败"要分开。** xfail 在 JUnit 里记成 ``skipped``
    （类型 ``pytest.xfail``），而测试报告工具**只看** ``failed``。这里把
    ``skipped`` 如实记成 ``skipped`` —— 报告因此是诚实的：它说"这条用例
    没跑成"，而不是把它说成"这条用例失败了"。
    """
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    tests: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        nodeid = f"{module_path(classname)}::{name}"
        outcome = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        tests.append({"nodeid": nodeid, "outcome": outcome})
    return tests


def module_path(classname: str) -> str:
    """``known_issues.test_known_issues`` → ``known_issues/test_known_issues.py``。"""
    parts = [part for part in classname.split(".") if part]
    if not parts:
        return "unknown.py"
    module = "/".join(parts[:-1])
    filename = f"{parts[-1]}.py"
    return f"{module}/{filename}" if module else filename


def candidate_of(state: LoopBState, candidate_id: str) -> EvolutionCandidate | None:
    """从这一轮发现的候选里找出那一条。"""
    discovery = state.discovery
    if discovery is None:
        return None
    for candidate in discovery.candidates:
        if candidate.id == candidate_id:
            return candidate
    return None


def task_key_for(spec: str | Callable[[str], str], candidate_id: str) -> str:
    """按候选 id 算出这个任务的标识。

    **为什么不是写死一个。** 任务标识是路线图里的 key，也是任务与决策、
    任务与执行结果之间那根线 —— 一轮派生多个任务时，标识必须**各不相同**，
    否则后一个会盖掉前一个，而盖掉这件事**不会报错**：路线图看上去仍然
    完整，只是少了一个任务。
    """
    return spec(candidate_id) if callable(spec) else spec


# --------------------------------------------------------------------------
# 驱动
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabaseTaskLookup:
    """任务表查询口 —— 给 Critic 的第三问用。

    **它只做"像"的筛选，不下"是不是同一件事"的结论。** 下结论是检查的事；
    端口自作主张地筛掉几条，Critic 就再也看不见它们了。
    """

    repository: TaskRepository
    project_id: str

    async def find_similar(self, text: str, *, limit: int = 20) -> tuple[TaskSummary, ...]:
        needle = text.strip()
        if not needle:
            return ()
        tasks = await self.repository.list_by_project(self.project_id)
        found = [
            TaskSummary(id=str(task.id), title=task.title, status=task.status)
            for task in tasks
            if needle in task.title
        ]
        return tuple(found[:limit])


async def _seed_project(session: AsyncSession, project: Project) -> None:
    """把项目行落库 —— **幂等**。

    **为什么必须先有项目行。** 周期、候选、任务、快照的 ``project_id`` 都是
    指向 ``project.id`` 的外键，而那些表一行都还没写 —— 没有项目行，第一
    条外键就过不去。

    **幂等是必须的。** 演示会反复跑；重复插一行会在第二次撞上主键冲突，
    报出来的是"违反唯一约束"，看不出是"这个项目已经登记过了"。
    """
    existing = await session.get(ProjectRow, project.id)
    if existing is not None:
        return
    session.add(ProjectRow.from_domain(project))
    await session.flush()


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    """这一轮跑完之后攒下的东西 —— 汇总时的输入。"""

    config: CycleConfig
    cycle_id: str
    state: LoopBState
    planner: Planner
    ledger: RunLedger
    elapsed: Decimal


def collect_result(outcome: CycleOutcome) -> CycleResult:
    """把这一轮的最终状态收成一份结论。

    **每一样都从"真的落过库的东西"里取。** 取不到的（比如没有决策、没有
    任务）就给空串 —— 空串在报告里是"这一项没有"，而不是"这一项没查"。

    **一轮可能有多个候选、多个决策、多个任务。** 顶层那几栏（候选 id、
    决策 id、任务标识）指的是**第一个** —— 它们是"这一轮的主线"，而全部
    的候选、决策与任务都在下面那几个元组里摊开。**只报第一个就等于藏起
    其余的**：读的人会以为这一轮只处理了一件事。
    """
    cycle_id = outcome.cycle_id
    final = outcome.state
    planner = outcome.planner
    ledger = outcome.ledger
    elapsed = outcome.elapsed

    candidates = _candidate_summary(final)
    decisions = _decision_summary(final)
    candidate_id = candidates[0][0] if candidates else ""

    decision_id = ""
    if final.decisions is not None and final.decisions.decisions:
        decision_id = final.decisions.decisions[0].id

    task_key = ""
    task_id = ""
    if final.roadmap is not None and final.roadmap.tasks:
        task_key = final.roadmap.tasks[0].key
        task_id = final.task_rows.get(task_key, "")

    accepted = False
    if final.execution is not None and final.execution.tasks:
        accepted = final.execution.tasks[0].accepted

    verdict, reasons, checks = _round_verdict(ledger)
    before = final.outcome.attainment_before if final.outcome is not None else None
    after = final.outcome.attainment_after if final.outcome is not None else None
    delta = None if before is None or after is None else after - before
    cycle_verdict = final.outcome.verdict if final.outcome is not None else None

    return CycleResult(
        project_id=final.project_id,
        cycle_id=cycle_id,
        cycle_ref=final.cycle_ref,
        candidate_id=candidate_id,
        decision_id=decision_id,
        task_key=task_key,
        task_id=task_id,
        approval_request_id=ledger.approval_request_id,
        verdict=verdict,
        reasons=reasons,
        checks=checks,
        accepted=accepted,
        merged=ledger.merged,
        merge_commit=ledger.merge_commit,
        workspace_path=ledger.workspace_path,
        branch=ledger.branch,
        changed_files=ledger.changed_files,
        elapsed_seconds=elapsed,
        cost=ledger.cost,
        currency=ledger.currency,
        records={
            "evolution_cycle": final.cycle_ref,
            "evolution_candidate": final.candidate_rows.get(candidate_id, ""),
            "evolution_decision": final.decision_rows.get(decision_id, ""),
            "task": task_id,
            "project_snapshot": final.knowledge.snapshot_ref if final.knowledge else "",
            "critic_review": "已审查" if planner.reports else "未审查",
        },
        visited_nodes=tuple(node.value for node in final.visited_nodes),
        cycle_outcome=cycle_verdict.outcome.value if cycle_verdict is not None else "",
        cycle_reason=cycle_verdict.reason if cycle_verdict is not None else "",
        critic_outcomes=tuple(
            (item.check.value, item.verdict.value, item.summary)
            for report in planner.reports
            for item in report.outcomes
        ),
        candidates=candidates,
        decisions=decisions,
        candidate_rows=dict(final.candidate_rows),
        decision_rows=dict(final.decision_rows),
        task_rows=dict(final.task_rows),
        task_ledgers=tuple(ledger.tasks),
        attainment_before=before,
        attainment_after=after,
        attainment_delta=delta,
    )


def _candidate_summary(final: LoopBState) -> tuple[tuple[str, str, str], ...]:
    """这一轮发现的全部候选 —— ``(候选 id, 标题, 置信度)``。"""
    discovery = final.discovery
    if discovery is None:
        return ()
    return tuple((item.id, item.title, str(item.confidence)) for item in discovery.candidates)


def _decision_summary(final: LoopBState) -> tuple[tuple[str, str, str], ...]:
    """这一轮的全部决策 —— ``(候选 id, 决策, 理由)``。

    **三态都要在。** defer 与 reject 同样是这一轮的结论 —— 只报 record
    的话，读的人看到的"这一轮做了什么"里就少了"这一轮决定不做什么"。
    """
    decisions = final.decisions
    if decisions is None:
        return ()
    return tuple(
        (item.candidate_id, item.decision.value, item.rationale) for item in decisions.decisions
    )


def _round_verdict(
    ledger: RunLedger,
) -> tuple[VerificationVerdict, tuple[str, ...], tuple[tuple[str, bool], ...]]:
    """这一轮的验证结论 —— **按任务合起来**。

    **全过就报全过；有一条没过就报那一条的结论。** 把最后一个任务的结论
    当成整轮的结论，听上去像那么回事，实际是**看运气** —— 前面的任务
    失败了而最后一个碰巧过了，这一轮就会报"通过"。

    **一条任务都没跑就说"没有拿到验证结论"**，不报"通过" —— 没验过与验
    过了是两件事。
    """
    if not ledger.tasks:
        report = ledger.verification
        if report is None:
            return VerificationVerdict.ERROR, ("没有拿到验证结论",), ()
        return report.verdict, report.reasons, report.checks
    failed = [item for item in ledger.tasks if item.verdict != VerificationVerdict.PASSED.value]
    if failed:
        reasons: list[str] = []
        for item in failed:
            reasons.extend(item.reasons)
        return (
            VerificationVerdict(failed[0].verdict),
            tuple(reasons),
            failed[0].checks,
        )
    return (
        VerificationVerdict.PASSED,
        tuple(reason for item in ledger.tasks for reason in item.reasons),
        ledger.tasks[0].checks,
    )


async def run_cycle(
    config: CycleConfig,
    recipe: CycleRecipe,
    session_factory: async_sessionmaker[AsyncSession],
) -> CycleResult:
    """**跑完一整轮闭环** —— 从观察到验证通过。

    这一轮的全过程：

    1. 读项目定义（``project.yaml``）；
    2. 把仓库复制成一份独立的 git 仓库，**原目录一个字都不动**；
    3. 跑一遍复现套件，产出 Critic 要看的测试报告；
    4. 落项目行（外键的前置条件）；
    5. 走循环 B：观察 → 快照 → 差距 → 发现 → 证据 → 评估 → 决策 → 记录 →
       计划 → 拆解落库 → 排期 → 执行 → 评估结果 → 更新知识 → 更新差距；
    6. 循环 A 在拆解落库之后接手：工作区 → 执行 → **独立验证（真跑 pytest）**
       → 验收判定 → 合并闸门 → 回写 → 收尾；
    7. 汇总结论 —— 每一样都能对账。

    **要一个真的 Postgres。** 验收口径是"每一步的 DB 记录可查" ——
    那些记录得**真的**在数据库里。链路里被替换掉的只有"写代码"那一段
    （见 :mod:`apps.director.m1_executor`），落库不是。

    :param config: 这一轮的配置。
    :param recipe: 这一轮的脑子 —— 谁观察、谁发现、怎么评分。
    :param session_factory: 数据库会话工厂。
    :raises RuntimeError: 链路本身跑不动时（那一轮没发生，不是"没做"）。
    """
    started = time.monotonic()
    project = load_project(config.project_file)
    project_id = config.project_id or project.id
    work_root = config.work_root
    work_root.mkdir(parents=True, exist_ok=True)
    cycle_id = config.cycle_id or new_cycle_id(recipe.cycle_prefix)
    executor_name = config.executor_name or recipe.executor_name
    if executor_name not in recipe.executors:
        raise LookupError(
            f"配方里没有执行器 {executor_name!r} —— 有的只有：{sorted(recipe.executors)}"
        )

    repo = materialize_pilot_repo(
        config.pilot_source,
        work_root / "repo",
        email=recipe.repo_email,
        name=recipe.repo_name,
    )
    report_path = write_test_report(repo, config.python_executable, work_root / "test-report.json")
    workspace_root = work_root / "workspaces"

    ledger = RunLedger()
    cycle_refs = CycleRefHolder()
    template = recipe.contract_template
    async with session_factory() as session:
        await _seed_project(session, project)
        # **项目行先提交。** 后面每一张表都拿它的 id 当外键，而外键检查要等
        # 被引用的行已提交才过得去。
        await session.commit()

        repository = TaskRepository(session)
        snapshots = SnapshotRepository(session)
        binder = DatabaseContractBinder(repository, template)
        journal = BindingJournal(EvolutionJournal(session), binder, session, cycle_refs)
        lookup = DatabaseTaskLookup(repository=repository, project_id=project_id)
        actuals = recipe.actuals(project) if recipe.actuals is not None else pilot_actuals(project)
        critic = build_critic(
            project=project,
            repo=repo,
            report_path=report_path,
            lookup=lookup,
            actuals=actuals,
        )
        planner = Planner(critic=critic, task_key=recipe.task_key)

        gate = ApprovalGate(
            PostgresApprovalStore(session_factory),
            ttl=timedelta(seconds=config.approval_ttl_seconds),
        )
        merge_gate = MergeGate(
            repo=repo,
            policy=PolicyManager(project.goal.autonomy),
            gate=gate,
            granter=HumanMergeGranter(gate, approver=recipe.approver, comment=recipe.merge_comment),
            ledger=ledger,
            merge_message=recipe.merge_message,
        )
        verifier = Verifier(
            engine=VerificationEngine(),
            registry=PostgresArtifactRegistry(session_factory),
            template=template,
            python_executable=config.python_executable,
            cycle_refs=cycle_refs,
            ledger=ledger,
            verified_by=recipe.verifier_name,
        )
        execution_journal = MirroringSessionJournal(DatabaseExecutionJournal(session), repository)
        loop_a_handlers = LoopAHandlers(
            planner=TaskPlanner(instruction=recipe.task_instruction, steps=recipe.task_steps),
            selector=Selector(executor_name=executor_name),
            workspace=Workspace(
                manager=WorkspaceManager(
                    repo_path=repo,
                    workspace_root=workspace_root,
                    git=SubprocessGitClient(),
                    clock=SystemClock(),
                    lease_ttl_seconds=config.lease_ttl_seconds,
                ),
                ledger=ledger,
                repository=repository,
                session=session,
            ),
            collector=Collector(ledger=ledger),
            verifier=verifier,
            diagnoser=Diagnoser(),
            merge_gate=merge_gate,
            project_state=ProjectState(repository=repository),
            journal=execution_journal,
            executors={executor_name: recipe.executors[executor_name]},
            repair_coordinator=RepairCoordinator(InMemoryRepairLedger(), limits=DEFAULT_LIMITS),
        )
        engineering = LoopAEngineeringCycle(
            loop_a_handlers,
            contract_bound_of=binder.is_bound,
            agent_id="director",
        )
        latest = await snapshots.latest(project_id)
        context = CycleContext(
            project=project,
            project_id=project_id,
            repo=repo,
            report_path=report_path,
            work_root=work_root,
            python_executable=config.python_executable,
            latest_snapshot_version=latest.version if latest is not None else 0,
        )
        brain = recipe.brain(context)
        loop_b_handlers = LoopBHandlers(
            observer=brain.observer,
            analyzer=brain.analyzer,
            discoverer=brain.discoverer,
            evaluator=brain.evaluator,
            decider=PolicyDecisionMaker(DecisionPolicy()),
            planner=planner,
            scheduler=Scheduler(
                executor_name=executor_name, repository=repository, session=session
            ),
            engineering=engineering,
            gap_updater=brain.gap_updater,
            journal=journal,
        )

        state = LoopBState(
            project_id=project_id,
            cycle_id=cycle_id,
            trigger=TriggerSource.QUALITY,
        )
        graph = build_loop_b_graph(loop_b_handlers)
        final = await run_loop_b(graph, state)
        await session.commit()

    elapsed = Decimal(str(round(time.monotonic() - started, 3)))
    return collect_result(
        CycleOutcome(
            config=config,
            cycle_id=cycle_id,
            state=final,
            planner=planner,
            ledger=ledger,
            elapsed=elapsed,
        )
    )


__all__ = [
    "ContractBinder",
    "ContractTemplate",
    "CycleBrain",
    "CycleConfig",
    "CycleContext",
    "CycleRecipe",
    "CycleRefHolder",
    "CycleResult",
    "DatabaseContractBinder",
    "DatabaseTaskLookup",
    "MergeGranter",
    "RunLedger",
    "TaskLedger",
    "TaskLookupPort",
    "build_critic",
    "collect_result",
    "diff_summary",
    "materialize_pilot_repo",
    "new_cycle_id",
    "pilot_actuals",
    "run_cycle",
    "sha256_of",
    "task_key_for",
    "verification_plan",
    "write_test_report",
]
