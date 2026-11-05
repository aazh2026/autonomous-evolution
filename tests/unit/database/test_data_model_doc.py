"""TASK-P1-020 ④ —— 数据模型文档与 ORM 模型保持一致。

T4 类交付物（``docs/architecture/data-model.md``）最容易腐坏：改了模型忘了改文档，
过几个月文档就在骗人。这里把文档钉在模型上：

- 十二张表都要在文档里出现；
- 每一条外键（子表.列 → 父表.列）都要在关系表里出现；
- 每张表的关键列都要在字段明细里出现；
- 三表分离、验收契约、append-only 这些**不可绕过的规则**必须在文档里被点名。

只断言"文档存在"是不够的 —— 那是一份空文件也能过的检查。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from core.entities import Base

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs/architecture/data-model.md"

#: 文档里用于定位各小节的标题。
_SECTION_OVERVIEW = "## 1. 实体总览"
_SECTION_RELATIONSHIPS = "## 2. 关系图"
_SECTION_RELATIONSHIP_TABLE = "### 2.1 文本版"
_SECTION_RELATIONSHIP_DIAGRAM = "### 2.2 图版"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"缺少数据模型文档：{DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def foreign_keys() -> list[tuple[str, str, str, str]]:
    """(子表, 子列, 父表, 父列) 四元组，顺序稳定。"""
    edges: list[tuple[str, str, str, str]] = []
    for table in Base.metadata.sorted_tables:
        for constraint in table.foreign_key_constraints:
            for element in constraint.elements:
                edges.append(
                    (
                        table.name,
                        element.parent.name,
                        element.column.table.name,
                        element.column.name,
                    )
                )
    return sorted(edges)


class TestTablesAreDocumented:
    """十二张表都要出现。"""

    def test_every_table_is_listed(self, doc_text: str) -> None:
        for table in Base.metadata.sorted_tables:
            assert table.name in doc_text, f"文档里没有 {table.name} 表"

    def test_no_phantom_tables(self, doc_text: str) -> None:
        """文档枚举表名的地方，不能出现模型里没有的表。"""
        real = set(Base.metadata.tables)
        # §1 总览表：`| 组 | 实体 | 表名 | 作用 |` —— 表名是第 3 列
        overview = _section(doc_text, _SECTION_OVERVIEW, _SECTION_RELATIONSHIPS)
        overview_names = {_cell_name(row, 2) for row in _table_rows(overview)}
        # §2.1 关系表：`| 子表 | 列 | 父表 | 父列 | ondelete |` —— 子表是第 1 列
        relationship = _section(
            doc_text, _SECTION_RELATIONSHIP_TABLE, _SECTION_RELATIONSHIP_DIAGRAM
        )
        relationship_names = {_cell_name(row, 0) for row in _table_rows(relationship)}
        assert overview_names, "总览表没解析出表名 —— 文档格式变了，测试要跟着改"
        assert relationship_names, "关系表没解析出表名 —— 文档格式变了，测试要跟着改"
        phantom = (overview_names | relationship_names) - real
        assert not phantom, f"文档提到了不存在的表：{sorted(phantom)}"


class TestRelationshipsAreDocumented:
    """每一条外键都要在关系表里写出来。"""

    def test_every_foreign_key_appears_in_the_relationship_table(
        self, doc_text: str, foreign_keys: list[tuple[str, str, str, str]]
    ) -> None:
        documented = _relationship_rows(doc_text)
        missing = [
            f"{child}.{child_col} → {parent}.{parent_col}"
            for child, child_col, parent, parent_col in foreign_keys
            if (child, child_col) not in documented
        ]
        assert not missing, f"关系表漏掉了这些外键：{missing}"

    def test_relationship_table_lists_exactly_the_real_relationships(
        self, doc_text: str, foreign_keys: list[tuple[str, str, str, str]]
    ) -> None:
        """反向检查：文档里写的每条关系都真实存在。"""
        documented = _relationship_rows(doc_text)
        real = {(child, col) for child, col, _, _ in foreign_keys}
        assert documented == real, (
            f"关系表与模型不一致；文档多出 {sorted(documented - real)}，"
            f"模型多出 {sorted(real - documented)}"
        )


class TestKeyColumnsAreDocumented:
    """design.md §3.6 点名的字段，一个都不能漏。"""

    #: design.md §3.6 表格里的「主要属性」，映射到实际列名。
    DESIGN_FIELDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "project": ("id", "goal", "policy"),
        "project_snapshot": ("version", "commit", "metrics"),
        "evolution_candidate": ("evidence", "value", "risk"),
        "evolution_decision": ("decision", "rationale"),
        "task": ("type", "priority", "status"),
        "task_dependency": ("predecessor_id", "successor_id"),
        "agent_session": ("executor", "session_id"),
        "execution_event": ("event_type", "payload"),
        "verification_run": ("checks", "result"),
        "artifact": ("uri", "hash", "type"),
        "architecture_decision": ("context", "decision", "tradeoff"),
        "evolution_cycle": ("cycle_id", "outcome"),
    }

    def test_design_named_fields_exist_in_the_model(self) -> None:
        for table_name, fields in self.DESIGN_FIELDS.items():
            columns = set(Base.metadata.tables[table_name].columns.keys())
            for field in fields:
                assert field in columns, f"{table_name} 缺少 design.md 点名的字段 {field}"

    def test_design_named_fields_are_documented(self, doc_text: str) -> None:
        for table_name, fields in self.DESIGN_FIELDS.items():
            detail = _section_for_table(doc_text, table_name)
            assert detail, f"文档里找不到 {table_name} 的字段明细小节"
            for field in fields:
                assert f"`{field}`" in detail, f"{table_name} 的字段明细里没有 {field}"


class TestNonBypassableRulesAreNamed:
    """不可绕过的规则必须在文档里被点名 —— 文档是给未来的人看的。"""

    @pytest.mark.parametrize(
        ("keyword", "why"),
        [
            ("decision_id", "三表分离：任务必须挂决策"),
            ("candidate_id", "三表分离：决策必须挂候选"),
            ("acceptance_contract", "R-051：验收契约是准入闸门"),
            ("append-only", "R-099：证据不可改写"),
            ("requested_analysis", "R-034：defer 必须说清还要补什么"),
        ],
    )
    def test_rule_is_documented(self, doc_text: str, keyword: str, why: str) -> None:
        assert keyword in doc_text, f"文档没有点名「{keyword}」—— {why}"


def _section(text: str, start: str, end: str) -> str:
    """取出两个标题之间的正文。"""
    assert start in text, f"文档里找不到小节标题 {start}"
    body = text.split(start, 1)[1]
    if end in body:
        body = body.split(end, 1)[0]
    return body


def _section_for_table(text: str, table_name: str) -> str:
    """取出文档里某个表的字段明细小节正文。"""
    pattern = rf"### 6\.\d+ `{re.escape(table_name)}`(.*?)(?=\n### |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1) if match else ""


def _relationship_rows(doc_text: str) -> set[tuple[str, str]]:
    """关系表里写出的 (子表, 子列) 对。"""
    section = _section(doc_text, _SECTION_RELATIONSHIP_TABLE, _SECTION_RELATIONSHIP_DIAGRAM)
    rows = []
    for row in _table_rows(section):
        child = _cell_name(row, 0)
        col = _cell_name(row, 1)
        if child and col:
            rows.append((child, col))
    return set(rows)


def _table_rows(section: str) -> list[list[str]]:
    """取出 Markdown 表格的**数据行**（跳过表头与分隔行）。

    按 ``|`` 切分再 strip —— 比正则稳：单元格里的粗体、反引号、箭头都不会
    影响切分结果。表头行与分隔行都不含反引号，用这一点区分。
    """
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not any("`" in cell for cell in cells):
            continue  # 表头行或分隔行
        rows.append(cells)
    return rows


def _cell_name(row: list[str], index: int) -> str:
    """取单元格里的标识符（去掉反引号与粗体）。取不到就返回空串。"""
    if index >= len(row):
        return ""
    return row[index].strip().strip("*` ").strip()
