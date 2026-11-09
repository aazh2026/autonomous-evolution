"""人工标注 —— 指标层唯一的 ground truth 来源。

设计依据：docs/tasks.md TASK-P1-083；docs/design.md §10 决策质量层。

**标注是人工给的。** 系统不能自己给自己打分 —— 那不是评测，是自我确认。
这一层只负责把那份人工结论**读出来、读准**，以及**读不到时如实说读不到**。

现在只有试点仓库一份标注集（:mod:`evaluation.labels.pilot`）。写这一层时
按"以后还会有别的仓库"来写：读取、解析、缺失的处理都在通用部分，只有"试点
仓库的标注文件在哪、一共几条"是试点特有的。
"""

from __future__ import annotations

from evaluation.labels.pilot import (
    EXPECTED_LABEL_COUNT,
    PILOT_LABELS_PATH,
    JsonlLabelSetStore,
    LabelSet,
    LabelSetError,
    LabelSetMissingError,
    LabelSetStore,
    PilotLabel,
    pilot_label_store,
)

__all__ = [
    "EXPECTED_LABEL_COUNT",
    "PILOT_LABELS_PATH",
    "JsonlLabelSetStore",
    "LabelSet",
    "LabelSetError",
    "LabelSetMissingError",
    "LabelSetStore",
    "PilotLabel",
    "pilot_label_store",
]
