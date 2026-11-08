"""TASK-P1-048 —— 验收证据归档。

两条硬断言（对应任务书 T1）：

1. **T1 ①** 验证完成后三类产物均可按 URI 取回且 hash 与登记值一致；
2. **T1 ②** 归档失败的验证**不计为通过**（fail-closed）。

第 2 条是这个模块存在的全部理由。归档失败如果还能算通过，那"证据不可丢失"
就只是一句口号 —— 通过的验收却没有证据，等于什么都没证明。而这条恰恰是最
容易被实现漏掉的地方：归档失败通常表现为"存不进去"，实现者很自然会想
"存不进去就跳过吧，结论还是通过的" —— 那样 fail-closed 就在实现里蒸发
了。所以这里把失败路径一条条钉死：取不到证据、上传失败、核对失败、登记
失败，四条路每一条都必须让结论不再是"通过"。

**为什么失败分 ``FAILED`` 与 ``ERROR`` 两种。** 前者是"验收口径没被满足"
（该返工），后者是"过程出错，结论不可得"（该让人看一眼）。把两者压成一
个"失败"，就会派去修一个根本没坏的东西 —— 这与
:mod:`harness.verification.engine` 里"没跑成 vs 跑了没过"的区分是同一条。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core.evidence.artifact_hash import ArtifactHashMismatchError, compute_hash
from core.evidence.artifacts import ArtifactKind, ArtifactRegistry, ArtifactStorage
from core.task.outcome import VerificationVerdict
from knowledge.artifacts.archiver import (
    ArchiveError,
    ArchiveRequest,
    ArtifactCollector,
    ArtifactNotFoundError,
    EvidenceArchiver,
    EvidenceSource,
)
from tests.unit.knowledge.artifacts.doubles import (
    FailingRegistry,
    FailingStorage,
    FakeArtifactRegistry,
    FakeArtifactStorage,
    LyingStorage,
    StubCollector,
    UnreadableStorage,
)

DIFF = b"diff --git a/x.py b/x.py\n+x = 1\n"
REPORT = b'{"result": "passed", "checks": 4}\n'
TRACES = b"unit_tests_pass: exit=0\nlint_pass: exit=0\n"

ALL_KINDS = frozenset(
    {ArtifactKind.GIT_DIFF, ArtifactKind.TEST_REPORT, ArtifactKind.EXECUTION_TRACE}
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _request(**overrides: object) -> ArchiveRequest:
    """一份"全都齐了"的归档请求。

    默认是**能通过**的那一种 —— 测试要盯的是"哪一条路会把结论改掉"，所以
    默认值得是"通过"，改动才显出方向。
    """
    payload: dict[str, object] = {
        "run_id": _uuid(),
        "task_id": _uuid(),
        "verdict": VerificationVerdict.PASSED,
        "verified_by": "verifier",
        "checks": ({"name": "unit_tests_pass", "passed": True},),
        "source": EvidenceSource(workspace=Path("/workspace")),
        "required": tuple(ALL_KINDS),
        "produced": ALL_KINDS,
    }
    payload.update(overrides)
    return ArchiveRequest(**payload)  # type: ignore[arg-type]


def _archiver(
    *,
    storage: ArtifactStorage | None = None,
    registry: ArtifactRegistry | None = None,
    collector: ArtifactCollector | None = None,
) -> EvidenceArchiver:
    return EvidenceArchiver(
        storage=storage or FakeArtifactStorage(),
        registry=registry or FakeArtifactRegistry(),
        collector=collector
        or StubCollector(
            {
                ArtifactKind.GIT_DIFF: DIFF,
                ArtifactKind.TEST_REPORT: REPORT,
                ArtifactKind.EXECUTION_TRACE: TRACES,
            }
        ),
    )


class TestTheThreeArtifactsAreRetrievable:
    """T1 ① —— 三类产物均可按 URI 取回且 hash 与登记值一致。"""

    async def test_all_three_kinds_are_archived(self) -> None:
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)

        result = await archiver.archive(_request())

        assert result.verdict is VerificationVerdict.PASSED
        assert result.passed is True
        assert result.archived is True
        assert {record.type for record in result.artifacts} == ALL_KINDS
        assert len(result.artifacts) == 3

    async def test_each_artifact_is_retrievable_by_uri(self) -> None:
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)

        result = await archiver.archive(_request())

        expected = {
            ArtifactKind.GIT_DIFF: DIFF,
            ArtifactKind.TEST_REPORT: REPORT,
            ArtifactKind.EXECUTION_TRACE: TRACES,
        }
        for record in result.artifacts:
            assert await archiver.retrieve(record.uri) == expected[record.type]

    async def test_retrieved_hash_matches_the_registered_hash(self) -> None:
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)

        result = await archiver.archive(_request())

        for record in result.artifacts:
            data = await archiver.retrieve(record.uri)
            assert compute_hash(data) == record.hash, "取回的内容与登记的摘要对不上"

    async def test_the_registry_holds_the_same_hash(self) -> None:
        registry = FakeArtifactRegistry()
        archiver = _archiver(registry=registry)

        result = await archiver.archive(_request())

        for record in result.artifacts:
            stored = await registry.get_by_uri(record.uri)
            assert stored is not None
            assert stored.hash == record.hash
            assert stored.hash == compute_hash(await archiver.retrieve(record.uri))

    async def test_artifacts_are_bound_to_the_verification_run(self) -> None:
        """``Artifact`` 与 ``VerificationRun`` 绑定 —— 这是交付物明写的一条。"""
        registry = FakeArtifactRegistry()
        archiver = _archiver(registry=registry)
        request = _request()

        result = await archiver.archive(request)

        for record in result.artifacts:
            assert record.verification_run_id == request.run_id
        listed = await registry.list_for(request.run_id)
        assert len(listed) == 3

    async def test_the_uri_points_at_the_stored_object(self) -> None:
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)

        result = await archiver.archive(_request())

        for record in result.artifacts:
            stored = await registry.get_by_uri(record.uri)
            assert stored is not None
            assert stored.uri.endswith(stored.hash)

    async def test_size_is_recorded(self) -> None:
        archiver = _archiver()

        result = await archiver.archive(_request())

        sizes = {record.type: record.size_bytes for record in result.artifacts}
        assert sizes[ArtifactKind.GIT_DIFF] == len(DIFF)
        assert sizes[ArtifactKind.TEST_REPORT] == len(REPORT)


class TestFailedArchiveIsNotPassed:
    """T1 ② —— 归档失败的验证不计为通过（fail-closed）。"""

    async def test_upload_failure_is_not_passed(self) -> None:
        archiver = _archiver(storage=FailingStorage())

        result = await archiver.archive(_request())

        assert result.passed is False
        assert result.verdict is not VerificationVerdict.PASSED
        assert result.archived is False
        assert result.artifacts == ()

    async def test_upload_failure_is_an_error_not_a_failure(self) -> None:
        """过程出错 → ``ERROR``（该让人看一眼），不是 ``FAILED``（该返工）。"""
        archiver = _archiver(storage=FailingStorage())

        result = await archiver.archive(_request())

        assert result.verdict is VerificationVerdict.ERROR

    async def test_missing_evidence_is_not_passed(self) -> None:
        """该有的证据没产出 → ``FAILED``（验收口径没被满足，该返工）。"""
        collector = StubCollector({ArtifactKind.GIT_DIFF: DIFF})
        archiver = _archiver(collector=collector)

        result = await archiver.archive(_request())

        assert result.passed is False
        assert result.verdict is VerificationVerdict.FAILED
        assert result.archived is False

    async def test_missing_evidence_names_the_missing_kind(self) -> None:
        collector = StubCollector({ArtifactKind.GIT_DIFF: DIFF})
        archiver = _archiver(collector=collector)

        result = await archiver.archive(_request())

        assert any("test_report" in reason for reason in result.reasons)

    async def test_a_collector_error_is_an_error_not_a_failure(self) -> None:
        """取证据的过程出错 → ``ERROR``（该让人看一眼），不是 ``FAILED``。

        与"证据没产出"要分得开：没产出是**事实**（那份东西不存在），出错是
        **不知道**（也许产出过，只是这会儿读不出来）。把"读不出来"说成
        "没有"，就会派去改一个根本没坏的代码。
        """
        collector = StubCollector(
            {
                ArtifactKind.TEST_REPORT: REPORT,
                ArtifactKind.EXECUTION_TRACE: TRACES,
            },
            errors={ArtifactKind.GIT_DIFF: "工作区读不出来"},
        )
        archiver = _archiver(collector=collector)

        result = await archiver.archive(_request())

        assert result.passed is False
        assert result.verdict is VerificationVerdict.ERROR
        assert any("git_diff" in reason for reason in result.reasons)

    async def test_registration_failure_is_not_passed(self) -> None:
        archiver = _archiver(registry=FailingRegistry())

        result = await archiver.archive(_request())

        assert result.passed is False
        assert result.verdict is VerificationVerdict.ERROR
        assert result.artifacts == ()

    async def test_tampered_write_is_not_passed(self) -> None:
        """写进去的内容与摘要对不上 → 不是通过。

        这一条对应真存储的一条**反直觉**性质：内容寻址的写是幂等的，
        key 已存在时**不重写** —— 包括那个对象已经被损坏的情况。所以
        ``put`` 之后必须自己 ``verify``；不核对就登记，等于把一份已经被
        换掉的内容登记成"证据"。
        """
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)

        # 先放一份"已经被换掉"的内容 —— key 由原内容的摘要决定。
        digest = compute_hash(DIFF)
        storage.tamper(f"sha256:{digest}", b"tampered")
        request = _request(
            produced=frozenset({ArtifactKind.GIT_DIFF}),
            required=(ArtifactKind.GIT_DIFF,),
        )

        result = await archiver.archive(request)

        assert result.passed is False
        assert result.verdict is VerificationVerdict.ERROR

    async def test_verification_failure_is_not_downgraded(self) -> None:
        """验证已经判了"没过"，就不能因为归档也失败而改成"没验成"。"""
        archiver = _archiver(storage=FailingStorage())

        result = await archiver.archive(_request(verdict=VerificationVerdict.FAILED))

        assert result.verdict is VerificationVerdict.FAILED

    async def test_verification_error_stays_error(self) -> None:
        archiver = _archiver()

        result = await archiver.archive(_request(verdict=VerificationVerdict.ERROR))

        assert result.verdict is VerificationVerdict.ERROR
        assert result.passed is False

    async def test_a_failed_verification_may_still_be_archived(self) -> None:
        """验证没过时证据**照样要归档** —— 失败的验收也要留证据。"""
        archiver = _archiver()

        result = await archiver.archive(_request(verdict=VerificationVerdict.FAILED))

        assert result.archived is True
        assert len(result.artifacts) == 3
        assert result.verdict is VerificationVerdict.FAILED

    async def test_archive_never_raises_on_runtime_failures(self) -> None:
        """运行期的失败**不抛异常**。

        抛异常的话，调用方一句 ``except Exception: pass`` 就能把 fail-closed
        吃掉；而"通过"这个结论只能从结果里读，结果里没有它，结论就丢不掉。
        这条用例的写法本身就是断言：**异常一旦冒出来，用例就红**。
        """
        archiver = _archiver(storage=FailingStorage())

        result = await archiver.archive(_request())

        assert result.passed is False

    async def test_the_callers_except_pass_cannot_eat_the_verdict(self) -> None:
        """把 fail-closed 写成一整段"调用方照做"的代码。

        这条用例模拟真实的调用方写法（``except Exception: pass``），断言
        **结论仍然是"不是通过"** —— 而不是被吞成"通过"。
        """
        archiver = _archiver(storage=FailingStorage())
        request = _request()

        passed = False
        try:
            result = await archiver.archive(request)
        except Exception:
            pass
        else:
            passed = result.passed

        assert passed is False

    async def test_the_recorded_run_result_is_the_combined_one(self) -> None:
        """落库的结论是**合起来**那个 —— fail-closed 必须是持久的。

        只在返回值里 fail-closed 不够：库里写着"通过"，而证据没归档上，
        事后看就是"验收通过了却没有证据"。
        """
        registry = FakeArtifactRegistry()
        collector = StubCollector({ArtifactKind.GIT_DIFF: DIFF})
        archiver = _archiver(registry=registry, collector=collector)
        request = _request()

        result = await archiver.archive(request)

        run = registry.run(request.run_id)
        assert run is not None
        assert run.result == result.verdict.value
        assert run.result == "failed"

    async def test_the_recorded_run_keeps_the_checks(self) -> None:
        registry = FakeArtifactRegistry()
        archiver = _archiver(registry=registry)
        request = _request()

        await archiver.archive(request)

        run = registry.run(request.run_id)
        assert run is not None
        assert run.checks == request.checks
        assert run.verified_by == request.verified_by


class TestRequiredVersusProduced:
    """契约要求的证据与这次产出的证据不是一回事。"""

    async def test_required_but_not_produced_is_reported(self) -> None:
        collector = StubCollector(
            {
                ArtifactKind.GIT_DIFF: DIFF,
                ArtifactKind.TEST_REPORT: REPORT,
            }
        )
        archiver = _archiver(collector=collector)

        result = await archiver.archive(_request())

        assert result.passed is False
        assert any("execution_trace" in reason for reason in result.reasons)

    async def test_produced_but_not_required_is_still_archived(self) -> None:
        """多出来的证据也归档 —— 它是这次验收的一部分。"""
        archiver = _archiver()

        result = await archiver.archive(
            _request(required=(ArtifactKind.GIT_DIFF,), produced=ALL_KINDS)
        )

        assert len(result.artifacts) == 3
        assert result.passed is True

    async def test_no_requirements_means_no_missing_evidence(self) -> None:
        collector = StubCollector({ArtifactKind.GIT_DIFF: DIFF})
        archiver = _archiver(collector=collector)

        result = await archiver.archive(_request(required=(), produced=frozenset()))

        assert result.passed is True
        assert result.archived is True


class TestWriteThenVerify:
    """写完必须自己核对一遍。"""

    async def test_verify_is_called_after_every_put(self) -> None:
        storage = FakeArtifactStorage()
        archiver = _archiver(storage=storage)

        await archiver.archive(_request())

        puts = [index for index, (name, _) in enumerate(storage.events) if name == "put"]
        verifies = [index for index, (name, _) in enumerate(storage.events) if name == "verify"]
        assert len(puts) == 3
        assert len(verifies) == 3
        for put_index, verify_index in zip(puts, verifies, strict=True):
            assert put_index < verify_index, "先写后核对 —— 顺序反了"

    async def test_nothing_is_registered_before_the_write_is_verified(self) -> None:
        registry = FakeArtifactRegistry()
        storage = FakeArtifactStorage()
        archiver = _archiver(storage=storage, registry=registry)

        await archiver.archive(_request())

        assert registry.events[0][0] == "record", "登记发生在核对之前"
        assert ("verify", storage.events[0][1]) in storage.events


class TestIdempotence:
    """重跑归档是常事 —— 重复归档不得搅浑"有几份证据"。"""

    async def test_the_same_content_lands_on_one_object(self) -> None:
        storage = FakeArtifactStorage()
        archiver = _archiver(storage=storage)

        await archiver.archive(_request())
        await archiver.archive(_request())

        assert storage.object_count() == 3, "同一份内容不该落下两份对象"

    async def test_the_same_run_does_not_register_twice(self) -> None:
        registry = FakeArtifactRegistry()
        archiver = _archiver(registry=registry)
        request = _request()

        await archiver.archive(request)
        await archiver.archive(request)

        assert registry.artifact_count() == 3

    async def test_a_fresh_run_registers_its_own_rows(self) -> None:
        registry = FakeArtifactRegistry()
        archiver = _archiver(registry=registry)

        await archiver.archive(_request())
        await archiver.archive(_request())

        assert registry.artifact_count() == 6


class TestRetrieval:
    """按 URI 取回证据。"""

    async def test_unknown_uri_raises_not_found(self) -> None:
        archiver = _archiver()

        with pytest.raises(ArtifactNotFoundError):
            await archiver.retrieve("memory://sha256:" + "0" * 64)

    async def test_an_unreadable_object_is_reported_as_not_found(self) -> None:
        """归档成功但取不回来 —— 报"取不到"，而不是把异常原样抛出去。

        抛出去的话，调用方得自己分辨"东西没了"与"存储坏了"；而这两种在
        归档这条路上都意味着同一件事：**这份证据不能用**。
        """
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        inner = _archiver(storage=storage, registry=registry)
        result = await inner.archive(_request())
        record = result.artifacts[0]

        archiver = _archiver(storage=UnreadableStorage(storage), registry=registry)

        with pytest.raises(ArtifactNotFoundError):
            await archiver.retrieve(record.uri)

    async def test_tampered_content_is_rejected_on_retrieval(self) -> None:
        """证据被换掉时必须**拒绝采信**，而不是把换掉的内容交出去。"""
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)
        result = await archiver.archive(_request())
        record = next(r for r in result.artifacts if r.type is ArtifactKind.GIT_DIFF)

        storage.tamper(f"sha256:{record.hash}", b"tampered")

        with pytest.raises(ArtifactHashMismatchError):
            await archiver.retrieve(record.uri)

    async def test_a_storage_that_lies_is_caught_by_the_archiver(self) -> None:
        """存储自己撒谎时，归档器必须**自己**再核对一遍。

        真存储读的时候会核对摘要，对不上就抛错 —— 那样归档器自己那道核对
        就永远走不到真问题。这里换一个**撒谎**的存储：它把别的内容交出来，
        还一脸没事。归档器不能把"存储说没事"当成"没事"。

        这条与上一条是**不同的洞**：上一条是存储自己发现了并报错；这一条是
        存储压根没发现 —— 能发现的只有归档器自己。
        """
        storage = FakeArtifactStorage()
        registry = FakeArtifactRegistry()
        archiver = _archiver(storage=storage, registry=registry)
        result = await archiver.archive(_request())
        record = next(r for r in result.artifacts if r.type is ArtifactKind.GIT_DIFF)

        liar = _archiver(
            storage=LyingStorage(storage, content=b"not the content that was registered"),
            registry=registry,
        )

        with pytest.raises(ArtifactHashMismatchError):
            await liar.retrieve(record.uri)


class TestRequestValidation:
    """请求本身不成立 —— 当场炸掉比归档出一份"看起来成功"的记录要好。"""

    async def test_empty_run_id_is_rejected(self) -> None:
        archiver = _archiver()

        with pytest.raises(ArchiveError):
            await archiver.archive(_request(run_id="  "))

    async def test_empty_task_id_is_rejected(self) -> None:
        archiver = _archiver()

        with pytest.raises(ArchiveError):
            await archiver.archive(_request(task_id=""))

    async def test_empty_verified_by_is_rejected(self) -> None:
        archiver = _archiver()

        with pytest.raises(ArchiveError):
            await archiver.archive(_request(verified_by=""))

    async def test_empty_checks_are_rejected(self) -> None:
        archiver = _archiver()

        with pytest.raises(ArchiveError):
            await archiver.archive(_request(checks=()))


class TestDeterminism:
    """次序必须是确定的 —— 报告里"先归档了 diff 还是先归档了报告"会被人拿来读。"""

    async def test_the_archive_order_is_by_kind_name(self) -> None:
        collector = StubCollector(
            {
                ArtifactKind.GIT_DIFF: DIFF,
                ArtifactKind.TEST_REPORT: REPORT,
                ArtifactKind.EXECUTION_TRACE: TRACES,
            }
        )
        archiver = _archiver(collector=collector)

        await archiver.archive(_request())

        assert [kind for _, kind in collector.events] == [
            ArtifactKind.EXECUTION_TRACE,
            ArtifactKind.GIT_DIFF,
            ArtifactKind.TEST_REPORT,
        ]
