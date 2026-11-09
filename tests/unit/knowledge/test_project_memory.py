"""TASK-P2-016 —— 项目记忆：跨轮次留下来的事实。

**这一层最要紧的性质是"上一轮做过什么，这一轮别忘了"。** 记忆的核心
场景是：一轮演进把 ``PILOT-001`` 修好了，下一轮的发现器不该再把它当
成新发现提出来。所以这里验的是：

1. **记下来就查得到** —— 键、摘要、详情、轮次都对得上；
2. **同一条事实再记一次 = 新修订，不是覆盖** —— 旧的那一版还在原地；
3. **"已经修好了"能一眼查到** —— ``resolved_issues()`` / ``is_resolved()``；
4. **坏文件不拖垮整个记忆** —— 解析不出来的那条被跳过。

**为什么用精确的键而不是相似度。** R-067 说得很直接：不全塞向量库，
不用对话历史当事实。"PILOT-001 已修复"要的是**精确命中** —— 摘要会
改，键不会。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from knowledge.project_memory.memory import (
    Fact,
    FactKind,
    FactStoreError,
    ProjectMemory,
    parse_fact,
    render_fact,
)


@pytest.fixture
def memory(tmp_path: Path) -> ProjectMemory:
    """一个空的项目记忆目录。"""
    return ProjectMemory(tmp_path / "memory")


class TestRecord:
    def test_a_recorded_fact_can_be_recalled(self, memory: ProjectMemory) -> None:
        """键、摘要、详情、轮次都对得上。"""
        memory.record(
            kind=FactKind.ISSUE_FIXED,
            key="PILOT-001",
            summary="已修复：tinylib.cli 没有测试覆盖",
            detail="补了 tests/test_cli.py",
            cycle_ref="cycle-7",
            tags=("pilot", "coverage"),
        )
        fact = memory.recall(FactKind.ISSUE_FIXED, "PILOT-001")
        assert fact is not None
        assert fact.key == "PILOT-001"
        assert fact.summary == "已修复：tinylib.cli 没有测试覆盖"
        assert fact.detail == "补了 tests/test_cli.py"
        assert fact.cycle_ref == "cycle-7"
        assert fact.tags == ("pilot", "coverage")

    def test_the_file_is_human_readable(self, memory: ProjectMemory) -> None:
        """前言放结构化字段，正文放人读的内容。"""
        memory.record(
            kind=FactKind.OBSERVATION,
            key="ARCH-001",
            summary="tinylib.cli 没有任何用例引用过",
            detail="从仓库索引里看出来的。",
        )
        path = memory.root / "observation" / "ARCH-001.md"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "kind: observation" in text
        assert "## " not in text  # 事实不是分节的文档
        assert "tinylib.cli 没有任何用例引用过" in text
        assert "从仓库索引里看出来的。" in text

    def test_an_empty_key_is_rejected(self, memory: ProjectMemory) -> None:
        """没有键的事实没法跨轮对照。"""
        with pytest.raises(FactStoreError):
            memory.record(kind=FactKind.OBSERVATION, key="  ", summary="摘要")

    def test_odd_key_characters_do_not_break_the_filename(self, memory: ProjectMemory) -> None:
        """**文件名只是索引，键本身不变。**"""
        memory.record(kind=FactKind.OBSERVATION, key="缺陷/一号", summary="摘要")
        fact = memory.recall(FactKind.OBSERVATION, "缺陷/一号")
        assert fact is not None
        assert fact.key == "缺陷/一号"
        assert (memory.root / "observation").exists()
        assert list((memory.root / "observation").glob("*.md"))


class TestRevisions:
    def test_recording_the_same_key_creates_a_revision(self, memory: ProjectMemory) -> None:
        """**旧的那一版留在原地。**"""
        first = memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="第一次的说法")
        second = memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="改过的说法")
        assert first.revision == 1
        assert second.revision == 2
        assert first.summary == "第一次的说法"

    def test_older_revisions_are_still_readable(self, memory: ProjectMemory) -> None:
        """历史不能被抹掉。"""
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="第一次的说法")
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="改过的说法")
        history = memory.history(FactKind.ISSUE_FIXED, "PILOT-001")
        assert [item.summary for item in history] == ["第一次的说法", "改过的说法"]

    def test_recall_returns_the_latest_revision(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="第一次的说法")
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="改过的说法")
        current = memory.recall(FactKind.ISSUE_FIXED, "PILOT-001")
        assert current is not None
        assert current.summary == "改过的说法"
        assert current.revision == 2

    def test_both_revisions_are_on_disk(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="第一次的说法")
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="改过的说法")
        names = sorted(path.name for path in (memory.root / "issue_fixed").glob("*.md"))
        assert names == ["PILOT-001.md", "PILOT-001.r2.md"]

    def test_different_keys_are_independent(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="一")
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-002", summary="二")
        first = memory.recall(FactKind.ISSUE_FIXED, "PILOT-001")
        second = memory.recall(FactKind.ISSUE_FIXED, "PILOT-002")
        assert first is not None
        assert second is not None
        assert first.revision == 1
        assert second.revision == 1


class TestCrossCycle:
    def test_resolved_issues_are_listed(self, memory: ProjectMemory) -> None:
        """**这是跨轮对照要用的那份清单。**"""
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="修好了")
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-002", summary="也修好了")
        memory.record(kind=FactKind.OBSERVATION, key="OBS-001", summary="一条观察")
        assert memory.resolved_issues() == ("PILOT-001", "PILOT-002")

    def test_is_resolved_answers_for_one_key(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="修好了")
        assert memory.is_resolved("PILOT-001") is True
        assert memory.is_resolved("PILOT-999") is False

    def test_an_unrecorded_issue_is_not_resolved(self, memory: ProjectMemory) -> None:
        assert memory.resolved_issues() == ()
        assert memory.is_resolved("PILOT-001") is False

    def test_recall_of_an_unknown_key_returns_none(self, memory: ProjectMemory) -> None:
        """ "读不出来"和"没有"要分得开 —— 这里都返回 ``None``。"""
        assert memory.recall(FactKind.ISSUE_FIXED, "没有这个") is None


class TestListing:
    def test_facts_returns_the_latest_revision_per_key(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="一")
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="二")
        memory.record(kind=FactKind.OBSERVATION, key="OBS-001", summary="三")
        facts = memory.facts()
        assert [(str(item.kind), item.key) for item in facts] == [
            ("issue_fixed", "PILOT-001"),
            ("observation", "OBS-001"),
        ]
        assert facts[0].summary == "二"

    def test_facts_can_be_filtered_by_kind(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.ISSUE_FIXED, key="PILOT-001", summary="一")
        memory.record(kind=FactKind.OBSERVATION, key="OBS-001", summary="二")
        assert memory.keys(FactKind.OBSERVATION) == ("OBS-001",)

    def test_an_empty_directory_lists_nothing(self, memory: ProjectMemory) -> None:
        assert memory.facts() == ()
        assert memory.keys(FactKind.ISSUE_FIXED) == ()


class TestRobustness:
    def test_a_corrupt_file_is_skipped(self, memory: ProjectMemory) -> None:
        """一个坏掉的事实不该让整个记忆读不出来。"""
        memory.record(kind=FactKind.OBSERVATION, key="OBS-001", summary="好的那条")
        (memory.root / "observation" / "broken.md").write_text("不是事实", encoding="utf-8")
        facts = memory.facts(FactKind.OBSERVATION)
        assert [item.key for item in facts] == ["OBS-001"]

    def test_a_corrupt_frontmatter_file_is_skipped(self, memory: ProjectMemory) -> None:
        memory.record(kind=FactKind.OBSERVATION, key="OBS-001", summary="好的那条")
        (memory.root / "observation" / "broken.md").write_text(
            "---\nkind: [\n---\n", encoding="utf-8"
        )
        assert len(memory.facts(FactKind.OBSERVATION)) == 1

    def test_an_unknown_kind_is_skipped(self, memory: ProjectMemory) -> None:
        """种类是有限的几种 —— 认不出来的不是事实。"""
        path = memory.root / "observation"
        path.mkdir(parents=True, exist_ok=True)
        (path / "weird.md").write_text(
            "---\nkind: 没见过的种类\nkey: X\nsummary: 摘要\n---\n正文\n", encoding="utf-8"
        )
        assert memory.facts(FactKind.OBSERVATION) == ()

    def test_files_sharing_a_name_do_not_mix(self, memory: ProjectMemory) -> None:
        """文件名相同但键不同的文件不能互相顶替。"""
        memory.record(kind=FactKind.OBSERVATION, key="A-1", summary="一")
        memory.record(kind=FactKind.OBSERVATION, key="A_1", summary="二")
        """``A-1`` 与 ``A_1`` 归一化成同一个文件名 —— 键得留在前言里。"""
        assert {item.key for item in memory.facts(FactKind.OBSERVATION)} == {"A-1", "A_1"}


class TestRendering:
    def test_render_and_parse_are_inverse(self) -> None:
        fact = Fact(
            kind=FactKind.CONSTRAINT,
            key="C-001",
            summary="摘要",
            detail="详情",
            recorded_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            cycle_ref="cycle-1",
            revision=3,
            tags=("a", "b"),
        )
        parsed = parse_fact(render_fact(fact))
        assert parsed is not None
        assert parsed.kind is FactKind.CONSTRAINT
        assert parsed.key == "C-001"
        assert parsed.summary == "摘要"
        assert parsed.detail == "详情"
        assert parsed.recorded_at == fact.recorded_at
        assert parsed.cycle_ref == "cycle-1"
        assert parsed.revision == 3
        assert parsed.tags == ("a", "b")

    def test_non_fact_text_does_not_parse(self) -> None:
        assert parse_fact("随便一段文字") is None
        assert parse_fact("") is None

    def test_a_fact_can_be_turned_into_a_document(self) -> None:
        fact = Fact(kind=FactKind.OBSERVATION, key="OBS-001", summary="摘要")
        document = fact.to_document()
        assert document["key"] == "OBS-001"
        assert document["kind"] == "observation"
        assert document["revision"] == 1
