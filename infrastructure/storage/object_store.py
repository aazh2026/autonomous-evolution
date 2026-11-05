"""产物对象存储。

设计依据：docs/design.md §3.6（产物库 S3/MinIO）、docs/tasks.md TASK-P1-004
（内容寻址 + 哈希校验）、TASK-P1-024（产物以 hash 寻址、篡改可被发现）。

分三层：

``ObjectStore``（抽象基类）    —— put/get/exists 的最小契约。两个实现：
``LocalObjectStore``             本地文件系统（开发、测试、单机部署）
``S3ObjectStore``                MinIO / S3（本地全栈与生产）
``ContentAddressedStore``       —— 内容寻址层：算 hash、按 hash 命名、读时校验

**为什么要有内容寻址这一层**：证据必须不可丢失、不可被悄悄换掉。
如果产物按"任务 id + 时间"命名，同一份内容会存成多份（去重不了），
而换掉内容后 URI 不变 —— 事后无法判断"这份证据还是不是当时那份"。
按内容 hash 命名之后：内容一样就是同一个对象（天然幂等），内容变了
URI 就变了，旧记录指向的仍是旧内容；读的时候再算一次 hash，被换掉当场发现。

**为什么校验发生在读的时候而不是写的时候**：写时校验只能证明"当时是对的"。
对象存储里的东西可能被覆盖、被重传、被运维脚本改掉 —— 没人读证据的时候
产物就已经被换掉了，校验和还是对的。读时校验才能证明"现在还是对的"。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.evidence.artifact_hash import (
    HASH_LENGTH,
    ArtifactHashMismatchError,
    compute_file_hash,
    compute_hash,
)
from infrastructure.config.settings import Settings, get_settings

#: 内容寻址 key 的前缀。完整 key 形如 ``sha256:<hex>``。
DIGEST_PREFIX = "sha256:"

#: 流式读写的块大小。1 MiB —— 足够大以减少调用开销，
#: 又小到不会把大产物整个读进内存。
CHUNK_SIZE = 1024 * 1024

#: HTTP 404。对象不存在时的状态码。
HTTP_STATUS_NOT_FOUND = 404

#: 逻辑 key 的分段分隔符。
#:
#: key 内部用 ``:`` 分段，**不允许出现** ``/``。这条限制换来两件事：
#: 本地后端可以把 ``:`` 映射成目录层级（``sha256:ab`` → ``sha256/ab``）
#: 而不丢信息，于是 key 可以从路径**反解**回来（``list_keys`` 需要）；
#: 路径穿越（``..``）也就无从谈起。
KEY_SEPARATOR = ":"

#: key 的分段分隔符 —— 只允许这一个，其余分隔符一律拒绝。
_PATH_SEPARATORS = frozenset({":", "/"})


class ObjectStoreError(RuntimeError):
    """对象存储操作失败。"""


class ObjectStoreUnavailableError(ObjectStoreError):
    """后端不可用（缺依赖、缺配置、连不上）。"""


class ObjectNotFoundError(ObjectStoreError):
    """对象不存在。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"对象不存在：{key}")
        self.key = key


@dataclass(frozen=True)
class StoredObject:
    """一次写入的结果。

    ``digest`` 是内容 sha256（十六进制，不带前缀），
    ``key`` 是内容寻址的完整 key（``sha256:<digest>``），
    ``uri`` 是这个对象在具体后端里的地址。

    ``skipped`` 表示"key 已存在，本次没有写入"。
    """

    key: str
    uri: str
    digest: str
    size_bytes: int
    #: 这次调用是否跳过了写入（同 key 的对象已存在）。
    skipped: bool = False


# --------------------------------------------------------------------------
# 抽象基类
# --------------------------------------------------------------------------


class ObjectStore(ABC):
    """对象存储的最小契约。

    刻意只定义"存取字节"和"判断存在"三件事 —— 覆盖写、删除、列举桶、
    设置生命周期这类能力都不在契约里。证据是 append-only 的
    （R-099 / I-05）：提供一个能删对象的接口，早晚有人会去删。
    """

    @abstractmethod
    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int:
        """写入字节内容，返回写入的字节数。"""

    @abstractmethod
    async def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> int:
        """从文件写入（流式，不整体读入内存），返回写入的字节数。"""

    @abstractmethod
    async def get_bytes(self, key: str) -> bytes:
        """读出完整内容。对象不存在时抛 :class:`ObjectNotFoundError`。"""

    @abstractmethod
    async def get_file(self, key: str, destination: Path) -> int:
        """流式下载到文件，返回写入的字节数。"""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """对象是否存在。"""

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        """列出前缀下的所有 key。用于核查"同一内容是否只存了一份"。"""

    @abstractmethod
    def uri(self, key: str) -> str:
        """对象在当前后端里的可引用地址。写进 ``artifact.uri`` 列。"""

    @abstractmethod
    async def ping(self) -> bool:
        """后端是否可用。健康检查用。

        语义是"这个后端现在能正常读写"，不是"某个 key 存在"。
        """


# --------------------------------------------------------------------------
# 本地文件系统后端
# --------------------------------------------------------------------------


class LocalObjectStore(ObjectStore):
    """本地文件系统后端。

    用途：单元测试、单机开发、不想装 Docker 时的最小可用形态。
    写操作是**原子**的 —— 先写同目录下的临时文件再原子改名。
    中途崩溃不会留下半截对象；半截对象在读时校验时会被判成"hash 不匹配"，
    看起来像是证据被篡改，那是假信号。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        return self._root / key_to_relative_path(key)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,  # noqa: ARG002
    ) -> int:
        # content_type 对文件系统没有意义 —— 参数只为满足接口契约。
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _atomic_writer(target) as handle:
            handle.write(data)
        return len(data)

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,  # noqa: ARG002
    ) -> int:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _atomic_writer(target) as handle, Path(path).open("rb") as source:
            shutil.copyfileobj(source, handle, CHUNK_SIZE)
        return target.stat().st_size

    async def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        return path.read_bytes()

    async def get_file(self, key: str, destination: Path) -> int:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source, destination.open("wb") as out:
            shutil.copyfileobj(source, out, CHUNK_SIZE)
        return destination.stat().st_size

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def list_keys(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            key = relative_path_to_key(path.relative_to(self._root))
            if key.startswith(prefix):
                keys.append(key)
        return keys

    def uri(self, key: str) -> str:
        return f"file://{self._path(key)}"

    async def ping(self) -> bool:
        return self._root.is_dir()


@contextmanager
def _atomic_writer(target: Path) -> Iterator[Any]:
    """写临时文件再原子改名。

    失败时删掉临时文件 —— 半截内容留在证据目录里，读的时候会被判成
    "hash 不匹配"，看起来像证据被篡改，那是假信号。
    """
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    tmp_path = Path(tmp_name)
    handle = os.fdopen(fd, "wb")
    try:
        yield handle
    except BaseException:
        handle.close()
        tmp_path.unlink(missing_ok=True)
        raise
    else:
        handle.close()
        tmp_path.replace(target)


# --------------------------------------------------------------------------
# S3 / MinIO 后端
# --------------------------------------------------------------------------


class S3ObjectStore(ObjectStore):
    """S3 兼容后端（MinIO / AWS S3）。

    依赖 ``aioboto3``（可选依赖组 ``s3``）。缺少时构造即报错并说明怎么装 ——
    不做"运行时才发现"的延迟失败。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._bucket = cfg.s3_bucket
        self._region = cfg.s3_region
        self._endpoint_url = cfg.s3_endpoint_url
        self._force_path_style = cfg.s3_force_path_style
        self._access_key = (
            cfg.s3_access_key.get_secret_value() if cfg.s3_access_key is not None else None
        )
        self._secret_key = (
            cfg.s3_secret_key.get_secret_value() if cfg.s3_secret_key is not None else None
        )
        self._client = client
        self._session: Any = None
        if client is None:
            try:
                # 延迟导入是**刻意的**：aioboto3 是可选依赖组 ``s3``，
                # 顶层导入会让没装该附加依赖的环境连本模块都 import 不了。
                import aioboto3  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - 取决于环境是否装了 s3 附加依赖
                raise ObjectStoreUnavailableError(
                    "S3 后端需要 aioboto3：uv sync --extra s3"
                ) from exc
            self._session = aioboto3.Session()

    @property
    def bucket(self) -> str:
        return self._bucket

    def _client_context(self) -> Any:
        """构造一个 S3 客户端上下文。

        注入 ``client`` 时直接返回它 —— 测试用假客户端，不碰网络。
        """
        if self._client is not None:
            return _InjectedClient(self._client)
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=_botocore_config(),
        )

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int:
        async with self._client_context() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type or "application/octet-stream",
            )
        return len(data)

    async def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> int:
        # Body 传文件句柄：botocore 会按块读取，不会把整个文件读进内存。
        with Path(path).open("rb") as body:
            async with self._client_context() as client:
                await client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentLength=Path(path).stat().st_size,
                    ContentType=content_type or "application/octet-stream",
                )
        return Path(path).stat().st_size

    async def get_bytes(self, key: str) -> bytes:
        chunks: list[bytes] = []
        async with self._client_context() as client:
            response = await self._get_object(client, key)
            await self._drain(response, chunks.append)
        return b"".join(chunks)

    async def get_file(self, key: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        async with self._client_context() as client:
            response = await self._get_object(client, key)
            with destination.open("wb") as out:
                written = await self._drain(response, out.write)
        return written

    async def _get_object(self, client: Any, key: str) -> Any:
        """发起 GET，把"对象不存在"翻译成本模块的异常。"""
        try:
            return await client.get_object(Bucket=self._bucket, Key=key)
        except _client_error() as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError(key) from exc
            raise

    async def _drain(self, response: Any, sink: Callable[[bytes], object]) -> int:
        """把响应体按块读出来，交给 sink。

        ``response["Body"]`` 是 aiobotocore 的流式体：``read()`` 是协程，
        而 ``async with`` 交给的是底层的 HTTP 响应 —— 两个不是一回事，
        所以块读要挂在 ``body`` 上而不是 ``async with`` 的产出物上。
        """
        body = response["Body"]
        written = 0
        async with body:
            while True:
                chunk = await body.read(CHUNK_SIZE)
                if not chunk:
                    break
                sink(chunk)
                written += len(chunk)
        return written

    async def exists(self, key: str) -> bool:
        async with self._client_context() as client:
            try:
                await client.head_object(Bucket=self._bucket, Key=key)
            except _client_error() as exc:
                if _is_not_found(exc):
                    return False
                raise
        return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        async with self._client_context() as client:
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"Bucket": self._bucket}
                if prefix:
                    kwargs["Prefix"] = prefix
                if token:
                    kwargs["ContinuationToken"] = token
                response = await client.list_objects_v2(**kwargs)
                for item in response.get("Contents", []):
                    keys.append(str(item["Key"]))
                if not response.get("IsTruncated"):
                    break
                token = response.get("NextContinuationToken")
                if not token:
                    break
        return sorted(keys)

    def uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    async def ping(self) -> bool:
        """桶在不在、能不能访问。

        桶不存在时 ``head_bucket`` 报 404 —— 那说明"后端连上了但没建桶"，
        对健康检查来说是不可用（写产物会失败）。
        """
        try:
            async with self._client_context() as client:
                await client.head_bucket(Bucket=self._bucket)
        except Exception:
            return False
        return True

    async def ensure_bucket(self) -> bool:
        """确保桶存在。返回是否新建。

        幂等：桶已存在时不报错。MinIO 默认没有桶，而 S3 的 ``put_object``
        不会替你建桶 —— 第一次写就 404 NoSuchBucket 的话，
        "配置错了"和"桶还没建"分不开。
        """
        async with self._client_context() as client:
            try:
                await client.head_bucket(Bucket=self._bucket)
            except _client_error() as exc:
                if not _is_not_found(exc):
                    raise
                await client.create_bucket(Bucket=self._bucket)
                return True
        return False


class _InjectedClient:
    """把注入的客户端包装成异步上下文管理器。

    测试传入的是自己写的假客户端，它不是 ``async with`` 上下文管理器；
    这里补上，让生产代码与测试代码走同一条路径。
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _botocore_config() -> Any:
    """botocore 的 path-style 配置。

    MinIO 用 path-style（``http://host:9000/bucket/key``）而不是
    virtual-host style —— 后者要求域名能解析到每个桶名，本地开发做不到。
    """
    from botocore.config import Config  # noqa: PLC0415

    return Config(signature_version="s3v4", s3={"addressing_style": "path"})


def _client_error() -> tuple[type[BaseException], ...]:
    """botocore 的 ClientError 类型。

    botocore 是 aioboto3 的依赖，装了 s3 附加依赖就一定在。
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    return (ClientError,)


def _is_not_found(exc: BaseException) -> bool:
    """是不是"对象不存在"。"""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    code = str(error.get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == HTTP_STATUS_NOT_FOUND


# --------------------------------------------------------------------------
# 内容寻址层
# --------------------------------------------------------------------------


class ContentAddressedStore:
    """内容寻址的产物存储。

    上层（证据归档、验收引擎）只跟这一层打交道，不直接碰后端：

    - 写入：内容 → sha256 → key ``sha256:<hex>``；同内容重复写**不产生第二个对象**。
    - 读取：取回内容重算 sha256，与 key 里的摘要比对；不一致就抛错。

    ``uri`` 是给 ``artifact.uri`` 列用的地址，``digest`` 是给 ``artifact.hash`` 列用的摘要。

    **已存在同 key 的对象时不重写** —— 包括内容已被损坏的对象。
    这是刻意的：证据被换掉是一个**要保留的信号**，悄悄用一份"看起来对"的
    内容覆盖上去，等于把篡改的痕迹抹掉。损坏在读的时候暴露
    （:class:`ArtifactHashMismatchError`），而不是在写的时候被顺手修好。

    代价是 ``put`` 的后置条件不是"key 处的内容 == 刚写入的内容"，
    而是"key 处存在一个对象"。需要确认内容完整的调用方，
    在 ``put`` 之后调 :meth:`verify` —— 归档流程（``TASK-P1-048``）必须这么做。
    """

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @property
    def backend(self) -> ObjectStore:
        return self._store

    # ---------- key ----------

    @staticmethod
    def key_for(digest: str) -> str:
        """由内容摘要得到内容寻址 key。"""
        return f"{DIGEST_PREFIX}{digest}"

    @staticmethod
    def digest_from_key(key: str) -> str:
        """从 key 反解出内容摘要。"""
        if not key.startswith(DIGEST_PREFIX):
            raise ValueError(f"不是内容寻址 key：{key!r}")
        digest = key[len(DIGEST_PREFIX) :]
        if len(digest) != HASH_LENGTH:
            raise ValueError(f"内容寻址 key 的摘要长度不对：{key!r}")
        return digest

    def uri_for(self, key: str) -> str:
        return self._store.uri(key)

    # ---------- 写 ----------

    async def put_bytes(self, data: bytes) -> StoredObject:
        """写入字节内容。

        幂等：同样的内容写多少次，库里都只有一份 —— 第二次直接跳过写入。
        """
        digest = compute_hash(data)
        key = self.key_for(digest)
        if await self._store.exists(key):
            return StoredObject(
                key=key,
                uri=self._store.uri(key),
                digest=digest,
                size_bytes=len(data),
                skipped=True,
            )
        await self._store.put_bytes(key, data, content_type="application/octet-stream")
        return StoredObject(
            key=key,
            uri=self._store.uri(key),
            digest=digest,
            size_bytes=len(data),
            skipped=False,
        )

    async def put_file(self, path: Path, *, content_type: str | None = None) -> StoredObject:
        """从文件写入（流式计算 hash，不整体读入内存）。"""
        source = Path(path)
        digest = compute_file_hash(source)
        key = self.key_for(digest)
        if await self._store.exists(key):
            return StoredObject(
                key=key,
                uri=self._store.uri(key),
                digest=digest,
                size_bytes=source.stat().st_size,
                skipped=True,
            )
        size = await self._store.put_file(key, source, content_type=content_type)
        return StoredObject(
            key=key,
            uri=self._store.uri(key),
            digest=digest,
            size_bytes=size,
            skipped=False,
        )

    # ---------- 读 ----------

    async def get_bytes(self, key: str) -> bytes:
        """读出内容并**校验 hash**。

        校验失败抛 :class:`ArtifactHashMismatchError` —— 证据被换掉了，
        调用方必须拒绝采信。
        """
        data = await self._store.get_bytes(key)
        expected = self.digest_from_key(key)
        actual = compute_hash(data)
        if actual != expected:
            raise ArtifactHashMismatchError(expected, actual, uri=self._store.uri(key))
        return data

    async def get_file(self, key: str, destination: Path) -> int:
        """流式下载到文件并校验 hash。"""
        expected = self.digest_from_key(key)
        await self._store.get_file(key, destination)
        actual = compute_file_hash(destination)
        if actual != expected:
            raise ArtifactHashMismatchError(expected, actual, uri=self._store.uri(key))
        return destination.stat().st_size

    async def verify(self, key: str) -> bool:
        """key 处的对象是否与它的摘要相符。

        返回 ``False`` 而不是抛错 —— 调用方往往只是想"确认一下"，
        真正要采信内容时用 :meth:`get_bytes`（那里失败会抛）。
        """
        try:
            await self.get_bytes(key)
        except ArtifactHashMismatchError:
            return False
        return True

    async def exists(self, key: str) -> bool:
        return await self._store.exists(key)


# --------------------------------------------------------------------------
# key ↔ 路径
# --------------------------------------------------------------------------


def key_to_relative_path(key: str) -> Path:
    """把逻辑 key 映射成本地相对路径。

    key 用 ``:`` 分段（``sha256:ab`` → ``sha256/ab``）。拒绝空段、``.``、
    ``..`` 与其它分隔符 —— key 来自外部输入（文件名、任务 id），不能让它
    写到证据目录外面去。
    """
    if not key:
        raise ValueError("对象 key 不能为空")
    parts: list[str] = []
    for segment in key.split(KEY_SEPARATOR):
        if segment in ("", ".", ".."):
            raise ValueError(f"非法的对象 key：{key!r}")
        if any(sep in segment for sep in _PATH_SEPARATORS - {KEY_SEPARATOR}):
            raise ValueError(f"非法的对象 key：{key!r}")
        parts.append(segment)
    return Path(*parts)


def relative_path_to_key(relative: Path) -> str:
    """把本地相对路径反解成逻辑 key。

    :func:`key_to_relative_path` 的逆运算。因为 key 的分段分隔符唯一，
    这个映射是可逆的。
    """
    return KEY_SEPARATOR.join(relative.parts)


def file_digest(path: Path) -> str:
    """文件的 sha256（流式）。给调用方省一次 import。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def build_object_store(settings: Settings | None = None) -> ObjectStore:
    """按配置选一个后端。

    配了 ``s3_endpoint_url``（或给了 S3 凭证）就用 S3，否则用本地文件系统。
    本地默认是文件系统 —— 少一个必需服务，"跑不起来"的理由就少一个。
    """
    cfg = settings or get_settings()
    if cfg.s3_endpoint_url or cfg.s3_access_key is not None:
        return S3ObjectStore(cfg)
    return LocalObjectStore(cfg.local_store_root)


def build_content_addressed_store(settings: Settings | None = None) -> ContentAddressedStore:
    """构造内容寻址存储。"""
    return ContentAddressedStore(build_object_store(settings))


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
