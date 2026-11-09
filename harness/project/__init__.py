"""项目状态的采集面 —— **真的去问 git、真的去读报告**。

设计依据：docs/tasks.md TASK-P1-023（采集时点的 commit SHA、代码统计、测试
结果、覆盖率）。

- :mod:`harness.project.snapshot_probes` —— 把 ``core/project/snapshot.py``
  声明的采集口变成真的：git、pytest 报告、coverage 报告、源码树统计。
"""

from __future__ import annotations

from harness.project.snapshot_probes import (
    COVERAGE_REPORT_ARTIFACT,
    IGNORED_DIRECTORIES,
    SOURCE_SUFFIXES,
    TEST_REPORT_ARTIFACT,
    CoverageJsonProbe,
    GitCommitProbe,
    PytestReportProbe,
    SourceTreeStatisticsProbe,
    SystemSnapshotClock,
)

__all__ = [
    "COVERAGE_REPORT_ARTIFACT",
    "IGNORED_DIRECTORIES",
    "SOURCE_SUFFIXES",
    "TEST_REPORT_ARTIFACT",
    "CoverageJsonProbe",
    "GitCommitProbe",
    "PytestReportProbe",
    "SourceTreeStatisticsProbe",
    "SystemSnapshotClock",
]
