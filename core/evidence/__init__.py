"""验收证据的完整性保障。

设计依据：docs/tasks.md TASK-P1-024（append-only 与不可丢失）。
"""

from __future__ import annotations

from core.evidence.artifact_hash import (
    HASH_ALGORITHM,
    HASH_LENGTH,
    ArtifactHashMismatchError,
    compute_file_hash,
    compute_hash,
    is_valid_hash,
    verify_hash,
)

__all__ = [
    "HASH_ALGORITHM",
    "HASH_LENGTH",
    "ArtifactHashMismatchError",
    "compute_file_hash",
    "compute_hash",
    "is_valid_hash",
    "verify_hash",
]
