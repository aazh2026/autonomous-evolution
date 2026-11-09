"""配置 —— 读一层键值，合并两层。

刻意用最简单的格式：一行一个 ``键=值``，``#`` 开头是注释。
"""

from __future__ import annotations

from pathlib import Path


def load_config(path):
    """读一个配置文件，返回一张字典。

    文件不存在时抛 :class:`FileNotFoundError`。
    """
    text = Path(path).read_text(encoding="utf-8")
    result = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"第 {number} 行不是 key=value：{stripped!r}")
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def merge(base, overlay):
    """把 ``overlay`` 叠在 ``base`` 上 —— 同名的键以 ``overlay`` 为准。

    **只有顶层是覆盖，不是深合并。** 深合并要问"两个都是字典时怎么办"，
    那个问题有多个合理答案，而配置文件通常不需要它。
    """
    merged = dict(base)
    for key, value in overlay.items():
        merged[key] = value
    return merged


def parse_bool(text, default=False):
    """把 ``yes`` / ``no`` / ``true`` / ``false`` 读成布尔值。"""
    lowered = str(text).strip().lower()
    if lowered in ("yes", "true", "1", "on"):
        return True
    if lowered in ("no", "false", "0", "off"):
        return False
    return default


__all__ = ["load_config", "merge", "parse_bool"]
