"""字符串小工具。

**这个模块刻意没有类型标注。** 它是试点仓库里"补类型标注"这类任务的
对象 —— 一个真实项目里总有几处新代码还没跟上。
"""

import re

WHITESPACE = re.compile(r"\s+")
NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize(text):
    """把空白折叠成单个空格并去掉首尾空白。"""
    return WHITESPACE.sub(" ", text).strip()


def slugify(text):
    """转成 URL 友好的短横线形式。

    非 ASCII 字母会被丢掉 —— 短横线形式的标识符本来就只该有 ASCII。
    """
    lowered = text.strip().lower()
    parts = NON_WORD.split(lowered)
    return "-".join(part for part in parts if part)


def truncate(text, limit):
    """截断到 ``limit`` 个字符以内。

    截断后加一个省略号 —— **省略号本身算在长度之内**，否则"截断到 10 个
    字符"会给出 11 个字符的结果。
    """
    if limit <= 0:
        raise ValueError(f"长度上限必须是正数：{limit}")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def word_wrap(text, width):
    """按宽度折行。"""
    if width <= 0:
        raise ValueError(f"宽度必须是正数：{width}")
    words = normalize(text).split(" ")
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = current + " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
