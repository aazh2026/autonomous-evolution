"""产物存储。

设计依据：docs/design.md §3.6、docs/tasks.md TASK-P1-004 / TASK-P1-024。
"""

from __future__ import annotations

from infrastructure.storage.object_store import (
    CHUNK_SIZE,
    DIGEST_PREFIX,
    KEY_SEPARATOR,
    ContentAddressedStore,
    LocalObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectStoreUnavailableError,
    S3ObjectStore,
    StoredObject,
    build_content_addressed_store,
    build_object_store,
    file_digest,
    key_to_relative_path,
    relative_path_to_key,
)

__all__ = [
    "CHUNK_SIZE",
    "DIGEST_PREFIX",
    "KEY_SEPARATOR",
    "ContentAddressedStore",
    "LocalObjectStore",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "ObjectStoreUnavailableError",
    "S3ObjectStore",
    "StoredObject",
    "build_content_addressed_store",
    "build_object_store",
    "file_digest",
    "key_to_relative_path",
    "relative_path_to_key",
]
