"""受管控记录的公用规矩 —— **文本、摘要、资格清单**。

设计依据：docs/tasks.md ``TASK-P4-060``（高风险变更治理）、``TASK-P4-070``（系统
自身改进提案机制）、R-061（高风险操作独立授权）。

**这一层只放"一件事该长什么样"，不判断任何一份具体的记录。** 它回答三个问题：

1. **这一项能不能是空的？** —— 不能（:func:`not_blank`）。
2. **这一项是不是等于没写？** —— "待补充"能过非空检查，却不算写清楚
   （:func:`specific_text`）。
3. **同一份内容永远是不是同一个摘要？** —— 是（:func:`content_digest`）。

**为什么它单独成一层。** 有两类受管控的记录 —— **系统自身改进提案**
（:mod:`core.evolution.proposal`）与**高风险变更**
（:mod:`core.evolution.high_risk_change`）—— 都要用这三条。而**它们之间的规矩
不该互相 import**：提案不该认识变更，变更也不该认识提案。**共同的上层放这里，
谁也不欠谁。**

**为什么摘要要用规范化 JSON 而不是拼字符串。** 拼字符串时"字段顺序变了、少了
一个冒号"都会算出另一个摘要，于是"内容其实没变"会被误判成"内容被改过"，而误
判的方向恰好是**把好的当成坏的** —— 放行被无端卡住。规范化 JSON 把字段顺序与
空白都定死。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from core.evolution.placeholders import is_placeholder
from core.evolution.risk import ReviewerScope


def content_digest(payload: Mapping[str, object]) -> str:
    """算一份内容的摘要 —— **同样的内容永远给同样的摘要**。

    **为什么摘要要算在"内容"上，而不是算在"这条记录"上。** 评审与批准绑的是
    **人当时看到的那一份改动**，不是"这条记录"这个身份 —— 记录的状态变了、多了
    一条评审，那还是同一份改动。反过来，内容改了（哪怕是被人手改了一下），
    摘要就变了，绑定在摘要上的评审与批准**全都失效**。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def not_blank(value: str, what: str) -> str:
    """这一项不能是空字符串 —— **返回去掉首尾空白后的那份**。"""
    text = value.strip()
    if not text:
        raise ValueError(f"{what}不能是空字符串")
    return text


def specific_text(value: str, what: str) -> str:
    """这一项必须写清楚 —— **占位写法不算**。

    **为什么连占位写法都拦。** "待补充"能通过非空检查，却等于没写。改了什么
    写成"待补充"，那份独立评审与那份批准批的就不是一件确定的事 —— 而它们本来
    是用来**确定**这件事的。
    """
    text = not_blank(value, what)
    if is_placeholder(text):
        raise ValueError(f"{what}不能是占位写法：{text!r}")
    return text


def unique_scopes(scopes: Sequence[ReviewerScope]) -> tuple[ReviewerScope, ...]:
    """把评审资格整理成**一份不重复、顺序稳定的**清单。

    **为什么要去重、为什么要定顺序。** 资格是"集合"语义 —— 同一个人既是架构
    评审人又是安全评审人，那**两项都算**，不是两项算一次。而顺序不稳定的话，
    "这份记录要求了哪些资格"就没法逐字比对（摘要、审计、看板都靠逐字比对）。
    """
    unique = set(scopes)
    return tuple(scope for scope in ReviewerScope if scope in unique)


__all__ = [
    "content_digest",
    "not_blank",
    "specific_text",
    "unique_scopes",
]
