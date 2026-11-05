"""TASK-P1-005 ④ —— 日志字段字典与实现保持一致。

文档类交付物最容易腐坏：改了代码忘了改文档，过几个月文档就在骗人。
这里把文档钉在代码上：

- 保留的上下文字段（可检索维度）必须两边都有；
- 脱敏键名单必须与实现一致 —— 文档少写一个键，就有人以为那个键不会被脱敏；
- 每条日志都有的标准字段必须在文档里出现。

只断言"文档存在"是不够的 —— 那是一份空文件也能过的检查。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.observability.logging import RESERVED_CONTEXT_KEYS, SENSITIVE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs/observability/log-schema.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"缺少日志字段字典：{DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


class TestReservedContextKeysAreDocumented:
    """可检索维度 —— 文档与实现必须一致。"""

    def test_reserved_keys_are_documented(self, doc_text: str) -> None:
        for key in RESERVED_CONTEXT_KEYS:
            assert f"`{key}`" in doc_text, f"日志字段字典里没有保留键 {key}"

    def test_doc_does_not_invent_extra_reserved_keys(self, doc_text: str) -> None:
        """文档不能承诺实现给不出的检索维度。"""
        section = doc_text.split("## 3. 保留的上下文字段", 1)[1].split("## 4.", 1)[0]
        documented = {
            cell.strip().strip("`")
            for line in section.splitlines()
            if line.strip().startswith("|")
            for cell in line.strip().strip("|").split("|")
            if cell.strip().strip("`").replace("_", "").isalnum()
        }
        invented = {key for key in documented if key.startswith(("task_", "cycle_", "project_"))}
        assert not (invented - set(RESERVED_CONTEXT_KEYS)), (
            f"文档承诺了实现没有的检索维度：{sorted(invented - set(RESERVED_CONTEXT_KEYS))}"
        )


class TestRedactionKeysAreDocumented:
    """脱敏名单 —— 少写一个就等于让人以为那个键安全。"""

    def test_every_sensitive_key_is_documented(self, doc_text: str) -> None:
        section = doc_text.split("## 5. 脱敏", 1)[1].split("## 6.", 1)[0]
        for key in SENSITIVE_KEYS:
            assert key in section, f"脱敏键 {key} 没写进日志字段字典"

    def test_doc_does_not_claim_more_than_is_redacted(self, doc_text: str) -> None:
        """文档不能把不脱敏的字段说成脱敏。"""
        section = doc_text.split("## 5. 脱敏", 1)[1].split("## 6.", 1)[0]
        for candidate in ("token_count", "duration_ms", "task_id"):
            assert candidate not in section or "匹配是**键名精确匹配**" in section


class TestStandardFieldsAreDocumented:
    """每条日志都有的字段。"""

    @pytest.mark.parametrize("field", ("event", "level", "timestamp", "service"))
    def test_standard_field_is_documented(self, doc_text: str, field: str) -> None:
        assert f"`{field}`" in doc_text, f"日志字段字典里没有标准字段 {field}"

    def test_degradation_markers_are_documented(self, doc_text: str) -> None:
        for marker in ("degraded", "langfuse_state", "reason"):
            assert f"`{marker}`" in doc_text, f"降级标记 {marker} 没写进日志字段字典"


class TestRetrievalExamples:
    """检索示例要用真实存在的字段 —— 否则照着做的人会白忙一场。"""

    def test_examples_use_documented_fields(self, doc_text: str) -> None:
        section = doc_text.split("## 7. 检索示例", 1)[1].split("## 8.", 1)[0]
        known = set(RESERVED_CONTEXT_KEYS) | {
            "event",
            "level",
            "timestamp",
            "service",
            "degraded",
            "langfuse_state",
            "api_key",
        }
        used = {
            token.strip("`\"' ")
            for token in section.replace("\n", " ").split()
            if "_" in token and token.strip("`\"' ").isidentifier()
        }
        unknown = {token for token in used if token not in known}
        assert not unknown, f"检索示例用了不存在的字段：{sorted(unknown)}"
