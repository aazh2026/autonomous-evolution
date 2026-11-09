"""TASK-P2-016 —— 架构决策存储：不可变，只能被取代。

**这一层最要紧的性质是"历史不能被抹掉"。** 决策一旦记下就**不再改写**
—— 要改就再记一条，让它取代旧的。所以这里验的是：

1. **写进去能原样读出来** —— 人读的那一份（YAML 前言 + Markdown 正文）
   不许在往返里丢字段；
2. **同一条决策不会被覆盖** —— 再记一次得到新 id，新文件；
3. **"已被取代"是推导出来的** —— 不回头改旧文件，而是从"谁指向了谁"
   推出来（``DecisionSet.effective()``）；
4. **坏文件不拖垮整个库** —— 解析不出来的那条被跳过，其余照常读。

**"取代链"是这里最实用的东西**：Agent 想知道"这个模块当初为什么这么
设计"时，要的是链子最底下那一条 —— 那通常写明了最初的理由。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.architecture.decisions import (
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    DecisionRecord,
    DecisionStore,
)
from knowledge.architecture.decisions import (
    DecisionError,
    DecisionSet,
    FileDecisionStore,
    decisions_as_set,
    parse_decision,
    render_decision,
)


@pytest.fixture
def store(tmp_path: Path) -> FileDecisionStore:
    """一个空的 ADR 目录。"""
    return FileDecisionStore(tmp_path / "adr")


class TestRoundTrip:
    async def test_a_decision_survives_a_round_trip(self, store: FileDecisionStore) -> None:
        """人读的那一份不许在往返里丢字段。"""
        written = await store.record(
            project_id="proj-1",
            context="索引每次都要重读整个仓库，太慢",
            decision="索引落盘，按 (大小, 修改时间) 判断是否需要重读",
            tradeoff="文件被改了就得多读一次",
        )
        loaded = await store.get(written.id)
        assert loaded is not None
        assert loaded.project_id == "proj-1"
        assert loaded.context == "索引每次都要重读整个仓库，太慢"
        assert loaded.decision == "索引落盘，按 (大小, 修改时间) 判断是否需要重读"
        assert loaded.tradeoff == "文件被改了就得多读一次"
        assert loaded.status == STATUS_ACCEPTED
        assert loaded.supersedes_id is None

    async def test_the_file_is_human_readable(self, store: FileDecisionStore) -> None:
        """前言放结构化字段，正文放人读的内容。"""
        written = await store.record(project_id="proj-1", context="背景在这里", decision="就这么定")
        text = (store.root / f"{written.id}.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "## 背景" in text
        assert "## 决策" in text
        assert "背景在这里" in text

    def test_render_and_parse_are_inverse(self) -> None:
        record = DecisionRecord(
            id="adr-0001",
            project_id="proj-1",
            context="背景",
            decision="决策",
            tradeoff="代价",
            status=STATUS_ACCEPTED,
            supersedes_id="adr-0000",
        )
        parsed = parse_decision(render_decision(record))
        assert parsed is not None
        assert parsed.id == "adr-0001"
        assert parsed.context == "背景"
        assert parsed.decision == "决策"
        assert parsed.tradeoff == "代价"
        assert parsed.supersedes_id == "adr-0000"


class TestImmutability:
    async def test_recording_twice_creates_a_new_decision(self, store: FileDecisionStore) -> None:
        """**历史不能被抹掉。** 同一条内容再记一次，是新的一条。"""
        first = await store.record(project_id="proj-1", context="背景", decision="决策")
        second = await store.record(project_id="proj-1", context="背景", decision="决策")
        assert second.id != first.id
        assert first.id == "adr-0001"
        assert second.id == "adr-0002"

    async def test_the_old_decision_is_untouched(self, store: FileDecisionStore) -> None:
        """记了新的之后，旧的那条还在，而且内容没动过。"""
        first = await store.record(project_id="proj-1", context="背景", decision="旧决策")
        await store.record(project_id="proj-1", context="背景", decision="新决策")
        loaded = await store.get(first.id)
        assert loaded is not None
        assert loaded.decision == "旧决策"

    async def test_identifiers_are_never_reused(self, store: FileDecisionStore) -> None:
        """**删掉的文件也不捡它的号。**

        重号会毁掉取代链 —— 有条决策写着"取代 adr-0001"，而 adr-0001
        被删掉又发了一个同号的，"谁取代了谁"就会指向不相干的决策，
        而且看不出来。宁可跳号。
        """
        first = await store.record(project_id="proj-1", context="背景", decision="第一条")
        await store.record(project_id="proj-1", context="背景", decision="第二条")
        (store.root / f"{first.id}.md").unlink()
        third = await store.record(project_id="proj-1", context="背景", decision="第三条")
        assert third.id != first.id
        assert third.id == "adr-0003"


class TestSupersedeChain:
    async def test_superseding_is_derived_from_the_link(self, store: FileDecisionStore) -> None:
        """**"已被取代"不用回头改旧文件。**"""
        first = await store.record(project_id="proj-1", context="背景", decision="旧决策")
        second = await store.record(
            project_id="proj-1",
            context="背景",
            decision="新决策",
            supersedes_id=first.id,
        )
        decisions = await store.recall("proj-1")
        effective = decisions_as_set(decisions).effective()
        assert [item.id for item in effective] == [second.id]

    async def test_the_chain_can_be_walked(self, store: FileDecisionStore) -> None:
        """链子的方向是"新 → 旧"，走到底是最早的那条。"""
        first = await store.record(project_id="proj-1", context="背景", decision="最初的决策")
        second = await store.record(
            project_id="proj-1",
            context="背景",
            decision="中间的决策",
            supersedes_id=first.id,
        )
        third = await store.record(
            project_id="proj-1",
            context="背景",
            decision="最新的决策",
            supersedes_id=second.id,
        )
        chain = decisions_as_set(await store.recall("proj-1")).chain(third.id)
        assert [item.id for item in chain] == [third.id, second.id, first.id]

    def test_unsuperseded_decisions_are_effective(self) -> None:
        decisions = DecisionSet(
            decisions=(
                DecisionRecord(id="adr-0001", project_id="p", context="背景", decision="一"),
                DecisionRecord(id="adr-0002", project_id="p", context="背景", decision="二"),
            )
        )
        assert [item.id for item in decisions.effective()] == ["adr-0001", "adr-0002"]


class TestRobustness:
    async def test_a_corrupt_file_does_not_break_the_rest(self, store: FileDecisionStore) -> None:
        """一个坏掉的 ADR 不该让整个事实库读不出来。"""
        await store.record(project_id="proj-1", context="背景", decision="好的那条")
        (store.root / "adr-9999.md").write_text("这不是 ADR", encoding="utf-8")
        found = await store.recall("proj-1")
        assert [item.decision for item in found] == ["好的那条"]

    async def test_a_corrupt_file_is_skipped_on_read(self, store: FileDecisionStore) -> None:
        await store.record(project_id="proj-1", context="背景", decision="好的那条")
        (store.root / "adr-9999.md").write_text("---\n坏的: [\n---\n", encoding="utf-8")
        assert len(await store.recall("proj-1")) == 1

    async def test_missing_decision_returns_none(self, store: FileDecisionStore) -> None:
        """ "读不出来"和"没有"要分得开 —— 但都别当成异常。"""
        assert await store.get("adr-4242") is None

    async def test_a_missing_directory_reads_empty(self, tmp_path: Path) -> None:
        empty = FileDecisionStore(tmp_path / "没有这个目录")
        assert await empty.recall("proj-1") == ()

    async def test_an_invalid_status_is_rejected(self, store: FileDecisionStore) -> None:
        """状态是有限的几种，不能随便写。"""
        with pytest.raises(DecisionError):
            await store.record(
                project_id="proj-1", context="背景", decision="决策", status="随便写的"
            )

    async def test_every_valid_status_is_accepted(self, store: FileDecisionStore) -> None:
        for status in (STATUS_ACCEPTED, STATUS_REJECTED, STATUS_SUPERSEDED):
            record = await store.record(
                project_id="proj-1", context="背景", decision="决策", status=status
            )
            loaded = await store.get(record.id)
            assert loaded is not None
            assert loaded.status == status


class TestPort:
    async def test_the_file_store_implements_the_port(self, store: FileDecisionStore) -> None:
        """**两套存储（文件 / 数据库）说的是同一件事。**"""
        assert isinstance(store, DecisionStore)

    async def test_recall_is_ordered_by_time(self, store: FileDecisionStore) -> None:
        first = await store.record(project_id="proj-1", context="背景", decision="第一条")
        second = await store.record(project_id="proj-1", context="背景", decision="第二条")
        found = await store.recall("proj-1")
        assert [item.id for item in found] == [first.id, second.id]

    async def test_recall_only_returns_own_project(self, store: FileDecisionStore) -> None:
        await store.record(project_id="proj-1", context="背景", decision="一")
        await store.record(project_id="proj-2", context="背景", decision="二")
        found = await store.recall("proj-1")
        assert [item.decision for item in found] == ["一"]
