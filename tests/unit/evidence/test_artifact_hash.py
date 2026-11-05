"""TASK-P1-024 ③ —— 产物 hash 校验。

核心验证点：**产物被篡改时校验必须失败并报错**，而不是"校验失败但没人管"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.evidence.artifact_hash import (
    HASH_LENGTH,
    ArtifactHashMismatchError,
    compute_file_hash,
    compute_hash,
    is_valid_hash,
    verify_hash,
)


class TestComputeHash:
    """hash 计算。"""

    def test_hash_is_sha256_hex(self) -> None:
        digest = compute_hash(b"evidence")
        assert len(digest) == HASH_LENGTH
        assert all(char in "0123456789abcdef" for char in digest)

    def test_known_value(self) -> None:
        """sha256("") 的公认值 —— 防止算法写错却"自洽"。"""
        assert (
            compute_hash(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_same_content_same_hash(self) -> None:
        assert compute_hash(b"abc") == compute_hash(b"abc")

    def test_different_content_different_hash(self) -> None:
        assert compute_hash(b"abc") != compute_hash(b"abd")


class TestVerifyHash:
    """校验。"""

    def test_matching_hash_passes(self) -> None:
        content = b'{"result": "passed"}'
        assert verify_hash(content, compute_hash(content)) == compute_hash(content)

    def test_tampered_content_raises(self) -> None:
        original = b'{"result": "passed"}'
        expected = compute_hash(original)

        with pytest.raises(ArtifactHashMismatchError) as exc_info:
            verify_hash(b'{"result": "failed"}', expected)

        assert exc_info.value.expected == expected
        assert exc_info.value.actual != expected

    def test_error_message_includes_uri(self) -> None:
        expected = compute_hash(b"a")
        with pytest.raises(ArtifactHashMismatchError) as exc_info:
            verify_hash(b"b", expected, uri="s3://bucket/report.json")
        assert "s3://bucket/report.json" in str(exc_info.value)

    def test_uppercase_expected_hash_is_accepted(self) -> None:
        """记录的 hash 是大写时也应该通过 —— 大小写不该决定成败。"""
        content = b"evidence"
        assert verify_hash(content, compute_hash(content).upper()) == compute_hash(content)

    def test_failure_raises_instead_of_returning_false(self) -> None:
        """失败必须抛错。返回 False 会被调用方忘记检查。"""
        with pytest.raises(ArtifactHashMismatchError):
            verify_hash(b"x", "0" * 64)


class TestFileHash:
    """文件 hash。"""

    def test_file_hash_matches_content_hash(self, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        payload = b'{"checks": 42}' * 1000
        path.write_bytes(payload)

        assert compute_file_hash(path) == compute_hash(payload)

    def test_large_file_is_hashed_in_chunks(self, tmp_path: Path) -> None:
        path = tmp_path / "big.bin"
        payload = b"x" * (3 * 1024 * 1024)
        path.write_bytes(payload)

        assert compute_file_hash(path) == compute_hash(payload)


class TestHashFormat:
    """hash 格式。"""

    def test_valid_hash(self) -> None:
        assert is_valid_hash("a" * 64)

    def test_too_short(self) -> None:
        assert not is_valid_hash("a" * 63)

    def test_non_hexadecimal(self) -> None:
        assert not is_valid_hash("z" * 64)

    def test_uppercase_is_valid(self) -> None:
        assert is_valid_hash("A" * 64)
