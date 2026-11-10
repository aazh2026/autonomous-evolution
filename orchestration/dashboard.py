"""跨周期趋势看板的编排 —— 取数、算指标、导出。

设计依据：docs/tasks.md TASK-P3-017（交付物"跨周期趋势看板（达成度、质量、
成本）"；验收"能导出 ≥7 天连续运行的四层指标趋势"；验证 T2）、R-117（不得用
任务数 / Token 数 / 提交量作核心指标）、R-118（四级指标体系）、I-13（成本
同源同币种）。

**这一层只做三件事**：把原料取回来、把看板算出来、把结果导出去。

**它不碰指标口径。** 口径在 :mod:`evaluation.trend` 里 —— 那一层是纯函数，
所以"第 D 天的点怎么算"能被单独证伪。而这一层要处理的是这一层自己的事：
标注基线在不在、导出成什么形态。

**标注基线不在，看板不会假装有。** 依赖人工标注的两项指标（有效问题发现
率、误报率）会报"基线未建立"，而不是"有效率 0%" —— 后者是一个会骗人的
结论。看板把这件事写进导出的**备注**里：读的人一眼就知道那两项不是"算出来
的 0"，而是"还没法算"。

**成本不换算。** 账本里记的是什么币种，看板就用什么币种 —— 两个币种的数
不能相加，这件事由指标层拦（它直接报错），而这一层不替它做决定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from evaluation.labels.pilot import LabelSet, LabelSetError, LabelSetMissingError, LabelSetStore
from evaluation.trend import TrendBoard, TrendInput, TrendWindow, build_board


#: 看板的原料来源 —— **一个协议，不是一个具体实现**。
#:
#: 编排层不该知道原料是从库里取的还是从测试夹具里造出来的：口径是纯函数，
#: 所以"给一组已知原料，看板算出什么"能被单独验 —— 而那件事需要一个能塞进
#: 假原料的来源。
class TrendSource(Protocol):
    """能取出一个项目的看板原料的东西。"""

    async def read(
        self,
        *,
        project_id: str,
        until: date,
        scope: str | None = None,
    ) -> TrendInput:
        """取出一个项目的全部原料，止于 ``until``（含）。"""
        ...


@dataclass(frozen=True, slots=True)
class DashboardExport:
    """一次导出的结果 —— 看板加上它的备注。

    **备注是看板的一部分，不是附言。** 少了标注基线这件事必须能被看见，
    否则读的人会把"基线未建立"当成"算出来是 0"。
    """

    board: TrendBoard
    notes: tuple[str, ...] = ()

    def to_json(self) -> str:
        """导出成 JSON —— **键排好序，两次写出来能逐字节比较**。"""
        payload = {**self.board.to_dict(), "notes": list(self.notes)}
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        """导出成 Markdown —— 给不看 JSON 的人读。"""
        text = self.board.to_markdown()
        if not self.notes:
            return text
        lines = [text, "", "## 备注", ""]
        lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class TrendDashboard:
    """看板的编排 —— 取数、算指标、导出。

    :param source: 原料来源。
    :param label_store: 人工标注基线的来源。**给了才标** —— 没标过的候选与
        决策一律报"基线未建立"，而不是"有效率 0%"。
    """

    source: TrendSource
    label_store: LabelSetStore | None = None

    async def build(
        self,
        *,
        project_id: str,
        window: TrendWindow,
        scope: str | None = None,
    ) -> DashboardExport:
        """造一块看板。

        :param scope: 成本归到哪个预算作用域。**默认是项目 id** —— 账本的
            ``scope`` 记的是"这笔钱算在谁头上"。
        """
        data = await self.source.read(project_id=project_id, until=window.end, scope=scope)
        labels, notes = self._load_labels()
        board = build_board(project_id, window, data, labels=labels)
        return DashboardExport(board=board, notes=notes)

    def _load_labels(self) -> tuple[LabelSet | None, tuple[str, ...]]:
        """取标注基线 —— **取不到就说清楚是哪一种取不到**。

        文件不在（要补标注）与文件坏了（要修文件）的处置方式不同，所以备注
        里要分得开。
        """
        if self.label_store is None:
            return None, ("没有接入人工标注基线 —— 依赖标注的两项指标报「基线未建立」。",)
        try:
            return self.label_store.load(), ()
        except LabelSetMissingError as exc:
            return None, (f"人工标注基线未建立（{exc}）—— 依赖标注的两项指标报「基线未建立」。",)
        except LabelSetError as exc:
            return None, (f"人工标注基线不可用（{exc}）—— 依赖标注的两项指标报「基线未建立」。",)


__all__ = ["DashboardExport", "TrendDashboard", "TrendSource"]
