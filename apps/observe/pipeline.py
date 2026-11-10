"""机会发现流水线 —— 六路观察接上，一份快照，一批候选。

设计依据：docs/tasks.md TASK-P3-013（Observe 层六类数据源接入 + Discover 层；
验收条件"六类数据源各接入 ≥1 个真实来源；候选的 evidence 指向真实可解析的
证据"）。

**这一层是接线的地方。** 上面那些模块各自都能用，但"六路接上、跑一遍、拿
到候选"这件事得有人真做一遍 —— 那就是这里。

**它返回两样东西，不是一样。** 快照说"看到了什么"，发现结果说"由此推出了
什么"。**只返回候选，就分不清"什么都没看到"与"看到了一堆但全被否了"** ——
而后者是需要有人看的。

**六路里有一路故意接得很浅。** 运行那一路只有探针才有料，没有探针它就是
空的；静态分析那一路只接了依赖图，没接依赖漏洞扫描。**接了几路、在哪儿
接的、哪些没接 —— 写在 docs/architecture/opportunity-pipeline.md 里**，不
写在代码注释里当秘密。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from apps.observe.resolvers import RepositoryEvidenceResolver
from apps.observe.sources import (
    ChangeHistorySource,
    DocumentationSource,
    FeedbackSource,
    PytestSuiteSource,
    RuntimeMetricSource,
    StaticAnalysisSource,
)
from apps.readers import RuntimeProbe
from core.evolution.observe import ObservationSnapshot
from core.evolution.opportunity import OpportunityDiscovery
from knowledge.architecture.model import build_architecture
from knowledge.repository_index.index import build_index
from orchestration.discover import OpportunityDiscoverer
from orchestration.observe import Observer

#: 问题单台账在仓库里的默认位置。
DEFAULT_LEDGER: Final[Path] = Path("docs/issues.md")

#: 决策记录目录在仓库里的默认位置。
DEFAULT_DOCS_DIRECTORY: Final[Path] = Path("docs/adr")


@dataclass
class PipelineConfig:
    """一条流水线的配置。

    :param project_id: 属于哪个项目。**不给就用仓库目录名。**
    :param targets: **跑哪些测试。必须说清楚** —— "跑整个项目"在不同项目里
        指的是不同的目录（试点仓库的缺陷复现套件在 ``known_issues/``，它自
        己的回归套件在 ``tests/``）。**跑错了地方得到的是"没有失败"，那是
        个假结论。**
    :param probes: 要看着的命令。**不给就空着** —— 运行那一路没有探针就什
        么也观察不到。
    :param ledger: 问题单台账（仓库相对路径）。
    :param docs_directory: 决策记录目录（仓库相对路径）。
    :param clock: 现在的时刻。
    """

    targets: tuple[str, ...]
    project_id: str | None = None
    probes: tuple[RuntimeProbe, ...] = ()
    ledger: Path = DEFAULT_LEDGER
    docs_directory: Path = DEFAULT_DOCS_DIRECTORY
    clock: Callable[[], datetime] | None = None


@dataclass
class OpportunityPipeline:
    """机会发现流水线 —— **观察进去，候选出来**。

    :param observer: 观察编排器。
    :param discoverer: 发现器。
    """

    observer: Observer
    discoverer: OpportunityDiscoverer

    def run(self) -> tuple[ObservationSnapshot, OpportunityDiscovery]:
        """跑一遍流水线。

        :return: (快照, 发现结果)。**两样都要看** —— 快照里"哪一路没读成"
            是发现结果解释不了的。
        """
        snapshot = self.observer.observe()
        return snapshot, self.discoverer.discover(snapshot)


def build_pipeline(root: Path, config: PipelineConfig) -> OpportunityPipeline:
    """把六路观察接成一条流水线。

    :param root: 仓库根目录。
    :param config: 配置。

    **六路都接上了，一路都不少。** 哪一路实际上读不到东西，是那一路自己的
    事（快照里会写清楚）—— **但没有哪一路是"没接"**。
    """
    architecture = build_architecture(build_index(root))
    sources = (
        ChangeHistorySource(root=root),
        PytestSuiteSource(root=root, targets=config.targets),
        StaticAnalysisSource(architecture=architecture),
        RuntimeMetricSource(probes=config.probes),
        FeedbackSource(root=root, ledger=config.ledger),
        DocumentationSource(root=root, directory=config.docs_directory),
    )
    observer = Observer(
        sources=sources,
        project_id=config.project_id or root.name,
        clock=config.clock,
    )
    resolver = RepositoryEvidenceResolver(root=root, issue_ledger=config.ledger)
    discoverer = OpportunityDiscoverer(resolver=resolver)
    return OpportunityPipeline(observer=observer, discoverer=discoverer)


__all__ = [
    "DEFAULT_DOCS_DIRECTORY",
    "DEFAULT_LEDGER",
    "OpportunityPipeline",
    "PipelineConfig",
    "build_pipeline",
]
