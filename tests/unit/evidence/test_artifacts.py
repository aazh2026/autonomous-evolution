"""TASK-P1-048 —— 验收证据的词汇与端口。

这个模块没有 IO，所以它的测试也只有两件事可做：**值域**与**不变量**。

值域：``ArtifactKind`` 就三种，``verification_run.result`` 就四个取值 ——
数据库那一列是自由文本（别的工具可能写过 ``log``），但本系统的词表是定死
的，认不出的值要当场暴露而不是被悄悄读成另一种。

不变量：**没跑检查的验收不算验收**。``checks`` 为空的验收记录如果被放行，
"通过"就成了一句没有依据的话。
"""

from __future__ import annotations

import pytest

from core.evidence.artifacts import (
    VERIFICATION_RESULTS,
    ArtifactKind,
    ArtifactRecord,
    VerificationRunRecord,
    digest_from_key,
    key_for,
)


class TestTheVocabulary:
    """产物种类与验收结果。"""

    def test_there_are_exactly_three_kinds(self) -> None:
        assert {kind.value for kind in ArtifactKind} == {
            "git_diff",
            "test_report",
            "execution_trace",
        }

    def test_the_result_values_match_the_database_constraint(self) -> None:
        assert frozenset({"passed", "failed", "error", "skipped"}) == VERIFICATION_RESULTS

    def test_an_unknown_result_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            VerificationRunRecord(
                run_id="r",
                task_id="t",
                checks=({"name": "x"},),
                result="unknown",
            )

    def test_a_run_without_checks_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            VerificationRunRecord(run_id="r", task_id="t", checks=())


class TestContentAddressing:
    """key 是内容的函数。"""

    def test_the_key_round_trips(self) -> None:
        digest = "a" * 64
        assert digest_from_key(key_for(digest)) == digest

    def test_the_key_carries_the_prefix(self) -> None:
        assert key_for("a" * 64).startswith("sha256:")

    @pytest.mark.parametrize("key", ["", "md5:" + "a" * 64, "sha256:short", "sha256:" + "a" * 63])
    def test_a_non_content_addressed_key_is_rejected(self, key: str) -> None:
        with pytest.raises(ValueError):
            digest_from_key(key)


class TestArtifactRecord:
    """登记记录。"""

    def test_the_type_comes_from_the_vocabulary(self) -> None:
        record = ArtifactRecord(
            artifact_id="a",
            task_id="t",
            verification_run_id="r",
            uri="memory://sha256:" + "b" * 64,
            hash="b" * 64,
            type=ArtifactKind.TEST_REPORT,
        )
        assert record.type.value == "test_report"

    def test_an_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ArtifactKind("log")
