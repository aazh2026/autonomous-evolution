"""词面匹配 —— 候选的措辞与"不做什么"的措辞对不对得上。

**为什么是词面，不是语义。** Critic 的非目标检查与约束检查要在没有模型的地方
给出**可复核**的否决理由。语义匹配（"这句话说的是不是那件事"）需要模型，而且
说不出"凭什么"。词面匹配只需要文本，而且能指出**是哪一段文字对上了** ——
读报告的人可以直接去看那一段对不对。

**它比语义更容易误报，也更容易漏报。** 换个说法的事挡不住（"移动端原生适配"
说成"手机端适配"就漏了），碰巧共用的词也会带偏。所以规则是**"宁可报出需要人
看一眼的信号，也不替人下结论"**：命中即否决，但报告里必须写清**对上了哪一段**
—— 复核的人一眼就能判断这个否决站不站得住。

**命中长度下限定在 4 个字符。** 两字通用词（"模块""数据""性能"）几乎每条候选
都会撞上，拿它们当否决理由，等于用噪声否决真候选。四字以上才多半是**同一个
专有说法**（"移动端""第三方支付""历史模块"）。
"""

from __future__ import annotations

from typing import Final

#: "不做什么"的措辞前缀 —— 从描述里剥掉它，剩下的才是"被禁的那件事"。
#:
#: 按长度从长到短排：先匹配"不做"再匹配"不"，"不做移动端适配"才不会被剥成
#: "做移动端适配"。没有否定前缀的描述（"本项目暂不引入新的第三方支付依赖"）
#: 整句都当作被禁对象 —— 公共子串照样能命中里面的"第三方支付"。
_PROHIBITION_MARKERS: Final[tuple[str, ...]] = tuple(
    sorted(
        (
            "暂不引入",
            "不允许",
            "不得",
            "禁止",
            "避免",
            "不做",
            "不重构",
            "不修改",
            "不引入",
            "不增加",
            "不使用",
            "不支持",
            "不改造",
            "不新增",
            "不替换",
            "不调整",
            "不触碰",
        ),
        key=len,
        reverse=True,
    )
)

#: 命中的最小公共子串长度。
DEFAULT_MIN_COMMON_LENGTH: Final[int] = 4

#: 剥掉前缀后要顺手去掉的标点。
_TRIM_CHARS: Final[str] = " 　，。、：；！？,. "


def forbidden_object(description: str) -> str:
    """从"不做 X"里取出 X。

    没有可识别的否定前缀时，整句都当作被禁对象 —— 少了前缀只是让匹配变宽，
    不会让该拦的漏掉。
    """
    text = description.strip()
    for marker in _PROHIBITION_MARKERS:
        if text.startswith(marker):
            remainder = text[len(marker) :].strip(_TRIM_CHARS)
            return remainder or text
    return text


def longest_common_substring(left: str, right: str) -> str:
    """两个字符串里最长的公共子串。

    用动态规划，复杂度 O(n·m) —— 描述与候选标题都是短文本，这个代价可以忽略，
    换来的是"命中了哪一段"能精确指出来。
    """
    if not left or not right:
        return ""

    previous = [0] * (len(right) + 1)
    best = ""
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > len(best):
                    best = left[i - current[j] : i]
        previous = current
    return best


def common_hit(left: str, right: str, *, min_length: int = DEFAULT_MIN_COMMON_LENGTH) -> str | None:
    """两段文本是否指向同一件事。命中则返回**对上的那一段**。

    返回 ``None`` 表示没命中 —— 调用方据此区分"没对上"与"对上了但对的是别处"。
    """
    hit = longest_common_substring(left, right)
    if len(hit) >= min_length:
        return hit
    return None


def match_forbidden(
    text: str,
    description: str,
    *,
    min_length: int = DEFAULT_MIN_COMMON_LENGTH,
) -> str | None:
    """候选文本是否触碰了这条"不做什么"。命中则返回对上的那一段。"""
    target = forbidden_object(description)
    if not target:
        return None
    return common_hit(text, target, min_length=min_length)


__all__ = [
    "DEFAULT_MIN_COMMON_LENGTH",
    "common_hit",
    "forbidden_object",
    "longest_common_substring",
    "match_forbidden",
]
