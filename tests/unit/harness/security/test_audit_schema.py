"""TASK-P1-045 ④ —— 审计字段字典与实现保持一致。

文档类交付物最容易腐坏：改了代码忘了改文档，过几个月文档就在骗人。
这里把文档钉在代码上：

- 审计记录的每个字段都必须在文档里；
- 文档不能承诺实现给不出的字段（写了实现里没有的字段，用它查审计的人
  会照着一个不存在的维度去查，然后一无所获）；
- 四要素必须被标成必填 —— 少一条要素的记录不算审计记录；
- 枚举取值必须齐全：少写一个取值，就有人以为那个结果不存在。

只断言"文档存在"是不够的 —— 那是一份空文件也能过的检查。
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from harness.security.audit import (
    NOT_APPLICABLE,
    AuditDecision,
    AuditEvent,
    AuditQuery,
    AuditResult,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DOC_PATH = REPO_ROOT / "docs/observability/audit-schema.md"

#: 审计记录的字段名。
EVENT_FIELDS = {field.name for field in fields(AuditEvent)}

#: 查询条件的字段名。
QUERY_FIELDS = {field.name for field in fields(AuditQuery)}

#: 四要素（R-062）—— 必须必填。
FOUR_ELEMENTS = ("agent_id", "task_id", "resource", "result")


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"缺少审计字段字典：{DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def _table_rows(section: str) -> list[list[str]]:
    """把一段 markdown 表格拆成行。"""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows


def _section(doc: str, start: str, end: str) -> str:
    """截出两节标题之间的正文。"""
    assert start in doc, f"文档里没有 {start} 这一节"
    body = doc.split(start, 1)[1]
    return body.split(end, 1)[0]


class TestTheFieldsAreDocumented:
    """记录的每个字段都要在字典里。"""

    def test_every_field_is_documented(self, doc_text: str) -> None:
        for name in sorted(EVENT_FIELDS):
            assert f"`{name}`" in doc_text, f"审计字段字典里没有字段 {name}"

    def test_the_dictionary_lists_exactly_the_implementation_fields(self, doc_text: str) -> None:
        """文档不能承诺实现给不出的字段。"""
        section = _section(doc_text, "## 2. 每条记录都有的字段", "## 3.")
        documented = {
            cells[0].strip("`")
            for cells in _table_rows(section)
            if cells and cells[0].startswith("`")
        }
        assert documented == EVENT_FIELDS, (
            f"字段字典与实现对不上：多出 {sorted(documented - EVENT_FIELDS)}，"
            f"少了 {sorted(EVENT_FIELDS - documented)}"
        )

    def test_the_four_elements_are_marked_as_required(self, doc_text: str) -> None:
        """四要素缺一条就不算审计记录 —— 字典里必须标成必填。"""
        section = _section(doc_text, "## 2. 每条记录都有的字段", "## 3.")
        required = {
            cells[0].strip("`"): cells[2]
            for cells in _table_rows(section)
            if cells and cells[0].startswith("`")
        }
        for name in FOUR_ELEMENTS:
            assert required[name] == "是", f"{name} 必须标成必填"

    def test_the_optional_fields_are_marked_as_optional(self, doc_text: str) -> None:
        section = _section(doc_text, "## 2. 每条记录都有的字段", "## 3.")
        required = {
            cells[0].strip("`"): cells[2]
            for cells in _table_rows(section)
            if cells and cells[0].startswith("`")
        }
        for name in sorted(EVENT_FIELDS - set(FOUR_ELEMENTS)):
            assert required[name] in {"是", "否"}, f"{name} 的必填状态说不清"


class TestTheValuesAreDocumented:
    """枚举取值与占位值。"""

    @pytest.mark.parametrize("value", [item.value for item in AuditResult])
    def test_every_result_is_documented(self, doc_text: str, value: str) -> None:
        assert f"`{value}`" in doc_text, f"审计字段字典里没有结果取值 {value}"

    @pytest.mark.parametrize("value", [item.value for item in AuditDecision])
    def test_every_decision_is_documented(self, doc_text: str, value: str) -> None:
        assert f"`{value}`" in doc_text, f"审计字段字典里没有判定取值 {value}"

    def test_the_placeholder_is_documented(self, doc_text: str) -> None:
        assert f"`{NOT_APPLICABLE}`" in doc_text, f"占位值 {NOT_APPLICABLE} 没写进字段字典"

    def test_the_denied_result_is_distinguished_from_failure(self, doc_text: str) -> None:
        """ "没开始"与"开始了但失败"不是一回事 —— 字典里要说清楚。"""
        section = _section(doc_text, "### `result`", "### `decision`")
        assert "denied" in section and "failure" in section
        assert "压根没开始" in section


class TestTheQueryDimensionsAreDocumented:
    """查询条件也要在字典里 —— 否则查的人不知道能按什么查。"""

    def test_every_query_field_is_documented(self, doc_text: str) -> None:
        section = _section(doc_text, "## 6. 查询维度", "## 7.")
        for name in sorted(QUERY_FIELDS):
            assert f"`{name}`" in section, f"查询维度里没有 {name}"


class TestTheRetrievalExamples:
    """检索示例要用真实存在的字段 —— 否则照着做的人会白忙一场。"""

    def test_the_examples_use_documented_fields(self, doc_text: str) -> None:
        section = _section(doc_text, "## 8. 检索示例", "## 9.")
        known = EVENT_FIELDS | QUERY_FIELDS
        used: set[str] = set()
        for cells in _table_rows(section):
            # 只看"="左边的键 —— 右边的值（TASK-042、"deny"）不是字段名。
            for condition in cells[-1].split(" and "):
                key = condition.split("=")[0]
                used.update(re.findall(r"[a-z_][a-z_0-9]*", key))
        unknown = {token for token in used if token not in known}
        assert not unknown, f"检索示例用了不存在的字段：{sorted(unknown)}"


class TestTheFailClosedRuleIsDocumented:
    """ "审计写失败就阻断"是这份字典最要紧的一条 —— 不能漏。"""

    def test_the_write_failure_rule_is_documented(self, doc_text: str) -> None:
        assert "## 7. 写失败即阻断" in doc_text

    def test_the_rule_forbids_auditing_failure_away(self, doc_text: str) -> None:
        section = _section(doc_text, "## 7. 写失败即阻断", "## 8.")
        # 折行会把这个短语拆开 —— 先把换行去掉再找。
        assert "审计失败就放行" in section.replace("\n", "")
