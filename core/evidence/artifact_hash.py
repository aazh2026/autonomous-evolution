"""产物内容校验。

设计依据：docs/design.md §3.6（Artifact 带 hash，产物以 hash 寻址）、
docs/tasks.md TASK-P1-024（产物被篡改时 hash 校验失败并报错）。

**为什么必须有这个模块**：对象存储里的东西可能被覆盖、被重传、被误改。
数据库里记着 sha256，但数据库不负责校验内容 —— 没人读证据的时候，
产物已经被换掉了，校验和还是对的。

所以校验必须发生在**读证据的那一刻**，而不是写入的时候。写入时校验
只能证明"当时是对的"，读时校验才能证明"现在还是对的"。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: 产物 hash 的算法。
HASH_ALGORITHM = "sha256"

#: sha256 的十六进制长度。
HASH_LENGTH = 64


class ArtifactHashMismatchError(ValueError):
    """产物内容与记录的 hash 不一致。

    证据被篡改，或存储损坏。两种情况的处理方式一样：**拒绝采信**。
    """

    def __init__(self, expected: str, actual: str, *, uri: str | None = None) -> None:
        location = f"（{uri}）" if uri else ""
        super().__init__(f"产物 hash 校验失败{location}：期望 {expected}，实际 {actual}")
        self.expected = expected
        self.actual = actual
        self.uri = uri


def compute_hash(content: bytes) -> str:
    """算内容的 sha256（十六进制小写）。"""
    return hashlib.sha256(content).hexdigest()


def compute_file_hash(path: Path) -> str:
    """算文件的 sha256。大文件分块读，不一次性载入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_hash(content: bytes, expected: str, *, uri: str | None = None) -> str:
    """校验内容 hash。

    返回实际 hash（一致时）；不一致时抛 :class:`ArtifactHashMismatchError`。
    失败就抛错 —— 返回 False 会让调用方忘记检查，把"证据可疑"当成
    "证据通过"。
    """
    actual = compute_hash(content)
    if actual != expected.lower():
        raise ArtifactHashMismatchError(expected, actual, uri=uri)
    return actual


def is_valid_hash(value: str) -> bool:
    """是不是一个格式合法的 sha256 十六进制串。"""
    if len(value) != HASH_LENGTH:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


__all__ = [
    "HASH_ALGORITHM",
    "HASH_LENGTH",
    "ArtifactHashMismatchError",
    "compute_file_hash",
    "compute_hash",
    "is_valid_hash",
    "verify_hash",
]
