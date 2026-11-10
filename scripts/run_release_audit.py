"""TASK-P4-060 的 T2 —— 从真库里取审批语料，送进新的放行闸里跑一遍，画成看板。

设计依据：docs/tasks.md ``TASK-P4-060``（高风险变更治理；验收条件"**标记为高风
险的变更 100% 经过独立评审后才可执行**"；验证 T1 + T2）、R-061（高风险操作独立
授权）。

**这个脚本不造数据。** 语料是真库里的 ``approval_request`` —— 那些审批是系统真
正放行过的。**判定走的是生产的那条路**（:class:`HighRiskReviewFlow` 的四步），
唯一被换掉的是存储。

**它回答三个问题。**

1. **照现在这样跑下去会怎样** —— 实测：新闸会把真语料里的哪些变更拦住。
2. **闸门会不会放行** —— 推演：份数齐了之后它放不放。一道只会拦不会放的闸，和
   没有闸是一回事。
3. **资格那一层有没有牙齿** —— 推演：把每一份都当作架构级改动。

**跑法**::

    python -m scripts.run_release_audit
    python -m scripts.run_release_audit --database-url postgresql+asyncpg://ases:ases@localhost:5432/ases

**产出**：

- ``docs/evaluation/release-ledger.jsonl`` —— **每一份变更在每一个场景下的结论**。
- ``docs/evaluation/release-audit-board.md`` —— 看板。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from evaluation.governance.release_audit import (
    RULE_LABELS,
    AuditScenario,
    LegacyGate,
    LiveApproval,
    ReleaseAuditReport,
    audit,
    legacy_gate,
    roster_from_corpus,
    scenario_architecture,
    scenario_as_is,
    scenario_paired,
)

#: 默认数据库 —— 与 docker-compose.yml 里的服务一致。
DEFAULT_DATABASE_URL = "postgresql+asyncpg://ases:ases@localhost:5432/ases"

#: 台账（JSONL）—— **每一份变更在每一个场景下的结论**。
DEFAULT_LEDGER = Path("docs/evaluation/release-ledger.jsonl")

#: 看板（Markdown）。
DEFAULT_BOARD = Path("docs/evaluation/release-audit-board.md")

#: 语料从哪儿来 —— **一句 SQL 抄在这儿，是为了能自己核对**。
#:
#: **注意那个 ``::text``。** ``approval_request.task_id`` 是字符串而 ``task.id``
#: 是 uuid —— 两边类型不一样，直接 join 会报"没有这个运算符"。
CORPUS_SQL = """
select a.id::text              as request_id,
       a.task_id               as task_id,
       coalesce(t.project_id, '')   as project_id,
       a.operation              as operation,
       a.status                 as status,
       a.requested_by           as requested_by,
       a.decided_by             as decided_by,
       coalesce(t.title, '')    as title,
       coalesce(a.diff_summary, '') as diff_summary,
       coalesce(a.evidence_ref, '') as evidence_ref,
       a.requested_at           as requested_at,
       a.decided_at             as decided_at
from approval_request a
left join task t on t.id::text = a.task_id
order by a.requested_at, a.id
"""


@dataclass(frozen=True, slots=True)
class _Outcome:
    """一次审计跑完的结果 —— **场景、报表、看板要写的行**。"""

    scenario: AuditScenario
    report: ReleaseAuditReport


async def _load_approvals(engine: AsyncEngine) -> tuple[LiveApproval, ...]:
    """从真库里把审批语料取回来。"""
    async with engine.connect() as conn:
        result = await conn.execute(text(CORPUS_SQL))
        rows = [dict(row) for row in result.mappings()]
    return tuple(
        LiveApproval(
            request_id=str(row["request_id"]),
            task_id=str(row["task_id"]),
            project_id=str(row["project_id"]),
            operation=str(row["operation"]),
            status=str(row["status"]),
            requested_by=str(row["requested_by"]),
            decided_by=None if row["decided_by"] is None else str(row["decided_by"]),
            title=str(row["title"]),
            diff_summary=str(row["diff_summary"]),
            evidence_ref=str(row["evidence_ref"]),
            requested_at=row["requested_at"],
            decided_at=row["decided_at"],
        )
        for row in rows
    )


async def _run(approvals: tuple[LiveApproval, ...]) -> list[_Outcome]:
    """三个场景挨个跑一遍 —— **名册按最松的一档登记**。"""
    roster = roster_from_corpus(approvals)
    outcomes: list[_Outcome] = []
    for scenario in (scenario_as_is(), scenario_paired(), scenario_architecture()):
        report = await audit(approvals, scenario=scenario, roster=roster)
        outcomes.append(_Outcome(scenario=scenario, report=report))
    return outcomes


def _write_ledger(path: Path, outcomes: list[_Outcome]) -> int:
    """把每一份变更在每一个场景下的结论写成一行一行。"""
    lines = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for outcome in outcomes:
            for verdict in outcome.report.verdicts:
                record = {
                    "scenario": outcome.report.scenario,
                    "measured": outcome.report.measured,
                    "change_id": verdict.change_id,
                    "task_id": verdict.task_id,
                    "project_id": verdict.project_id,
                    "operation": verdict.operation,
                    "kind": verdict.kind,
                    "risk": verdict.risk,
                    "required_reviews": verdict.required_reviews,
                    "approvers": list(verdict.approvers),
                    "rule": verdict.rule,
                    "reason": verdict.reason,
                }
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                lines += 1
    return lines


def _counter_text[Key](counter: Mapping[Key, int]) -> str:
    """把一个计数摊成人话。"""
    if not counter:
        return "（没有）"
    ordered = sorted(counter.items(), key=lambda item: str(item[0]))
    return "、".join(f"{name} × {count}" for name, count in ordered)


def _corpus_facts(approvals: tuple[LiveApproval, ...]) -> list[str]:
    """语料里有什么 —— **看板开头那几行**。"""
    operations = Counter(approval.operation for approval in approvals)
    statuses = Counter(approval.status for approval in approvals)
    requesters = Counter(approval.requested_by for approval in approvals)
    deciders = Counter(
        approval.decided_by for approval in approvals if approval.decided_by is not None
    )
    per_task = Counter(approval.task_id for approval in approvals)
    reviewers_per_task = Counter(per_task.values())
    moments = [approval.requested_at for approval in approvals if approval.requested_at]
    facts = [
        f"- 语料：**{len(approvals)}** 条审批请求，来自 ``approval_request`` 表",
        f"- 涉及任务：**{len(per_task)}** 个（**一个任务就是一次变更**）",
        f"- 操作分布：{_counter_text(operations)}",
        f"- 状态分布：{_counter_text(statuses)}",
        f"- 提出人：{_counter_text(requesters)}",
        f"- 表态人：{_counter_text(deciders)}",
        f"- **每个任务有几条审批**：{_counter_text(reviewers_per_task)}",
    ]
    if moments:
        window = f"{min(moments).isoformat()} → {max(moments).isoformat()}"
        facts.append(f"- 时间范围：{window}")
    return facts


def _render_legacy(legacy: LegacyGate) -> list[str]:
    """基线那一节。"""
    return [
        "## 现行规矩（基线）",
        "",
        "旧审批门只要求 somebody ≠ 提出人表态就放行 —— 不看有没有第二个人，",
        "不看这个人懂不懂。",
        "",
        "| 口径 | 条数 |",
        "| --- | --- |",
        f"| 审批请求总数 | {legacy.total} |",
        f"| 按旧规矩放行 | {legacy.released} |",
        f"| **提出人自己批了自己**（旧规矩拦不住） | {legacy.self_approved} |",
        "",
    ]


def _render_scenarios(outcomes: list[_Outcome]) -> list[str]:
    """三个场景的总表。"""
    lines = [
        "## 三个场景",
        "",
        "| 场景 | 性质 | 变更数 | 放行 | 拦住 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for outcome in outcomes:
        report = outcome.report
        nature = "**实测**" if report.measured else "推演"
        lines.append(
            f"| {report.scenario}（{report.label}） | {nature} | {report.total} | "
            f"{report.released} | {report.blocked} |"
        )
    lines.extend(
        [
            "",
            "**as-is 是实测** —— 照真语料原样送进新闸，量的是“按现在这样跑下去会怎样”。",
            "",
            "**paired 与 architecture 是推演** —— 推演不是实测：它们回答的是",
            "“闸门在该放的时候放不放”“资格那一层有没有牙齿”。",
            "",
        ]
    )
    return lines


def _render_rules(outcomes: list[_Outcome]) -> list[str]:
    """拦住的理由分布。"""
    lines = ["## 拦住的理由", ""]
    for outcome in outcomes:
        report = outcome.report
        lines.append(f"### {report.scenario}（{report.label}）")
        lines.append("")
        lines.append("| 规矩 | 条数 |")
        lines.append("| --- | --- |")
        ordered = sorted(report.rules.items(), key=lambda item: (-item[1], item[0]))
        for rule, count in ordered:
            lines.append(f"| {RULE_LABELS.get(rule, rule)}（`{rule}`） | {count} |")
        lines.append("")
    return lines


def _render_verdicts(outcomes: list[_Outcome]) -> list[str]:
    """每一份变更的结论 —— **实测那一栏逐条列，推演的去台账里看**。"""
    lines = ["## 每一份变更的结论", ""]
    for outcome in outcomes:
        report = outcome.report
        lines.append(f"### {report.scenario}（{report.label}）")
        lines.append("")
        if not report.measured:
            lines.append(
                f"推演场景 —— **{report.total} 份变更的逐条结论在台账里**"
                f"（按 ``scenario`` 字段筛）。"
            )
            lines.append("")
            continue
        lines.append("| 变更 | 任务 | 风险档 | 要几份 | 批过的人 | 结论 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for verdict in report.verdicts:
            reviewers = "、".join(verdict.approvers) or "（没有人）"
            conclusion = RULE_LABELS.get(verdict.rule, verdict.rule)
            lines.append(
                f"| `{verdict.change_id[:8]}` | `{verdict.task_id[:8]}` | "
                f"{verdict.risk} | {verdict.required_reviews} | {reviewers} | {conclusion} |"
            )
        lines.append("")
    return lines


def _render_caveats() -> list[str]:
    """没测的，和已知的不足。"""
    return [
        "## 没测的，和已知的不足",
        "",
        "- **语料里没有“改动种类”这一项** —— 所以“资格那一层”在真数据上答不了这个",
        "  问题，只能推演（architecture 场景）。",
        "- **语料里没有“评审人资格”这一项** —— 名册是按语料里出现过的人**一律**按最",
        "  松的一档（general）登记的。**这是对新闸最不利的假设**：在这个假设下它都",
        "  拦不住，那才是真的拦不住。",
        "- **旧审批没有绑内容摘要** —— 它绑的是任务。回放时把那份结论绑到这份变更的",
        "  摘要上：这份评审说的就是这一份改动，但“绑摘要”是新规矩加的那道绑定，",
        "  **不是这次量出来的东西**。",
        "- **这批语料里没有拒过的、没有过期的** —— “拒绝是终态”“过期盖不住拒绝”两",
        "  条在真数据上没有被触发，只有单元测试覆盖。",
        "- **这一轮只量了闸门** —— 闸门拦住之后流程怎么走（谁被通知、怎么补评审）",
        "  不在这次范围内。",
        "",
    ]


def _render_reproduce(ledger_path: Path, ledger_lines: int) -> list[str]:
    """怎么复现。"""
    return [
        "## 复现",
        "",
        "```",
        "python -m scripts.run_release_audit",
        "```",
        "",
        f"台账：``{ledger_path}``（{ledger_lines} 行，每行是一份变更在一个场景下的结论）",
        "",
    ]


def _render_board(
    approvals: tuple[LiveApproval, ...],
    outcomes: list[_Outcome],
    ledger_path: Path,
    ledger_lines: int,
) -> str:
    """画看板。"""
    lines = [
        "# 高风险放行审计 —— 真语料过一遍新闸",
        "",
        "本看板的每一个数都来自**真库里的审批记录**（``approval_request`` 表），",
        "判定走的是**生产的那条路**（``HighRiskReviewFlow`` 的四步）。",
        "",
    ]
    lines.extend(_corpus_facts(approvals))
    lines.append("")
    lines.extend(_render_legacy(legacy_gate(approvals)))
    lines.extend(_render_scenarios(outcomes))
    lines.extend(_render_rules(outcomes))
    lines.extend(_render_verdicts(outcomes))
    lines.extend(_render_caveats())
    lines.extend(_render_reproduce(ledger_path, ledger_lines))
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    """跑一遍。"""
    engine = create_async_engine(args.database_url, poolclass=NullPool, future=True)
    try:
        approvals = await _load_approvals(engine)
    finally:
        await engine.dispose()
    if not approvals:
        print("❌ 真库里没有审批语料 —— 这次审计没有可量的东西。", file=sys.stderr)
        return 1
    outcomes = await _run(approvals)
    ledger_lines = _write_ledger(args.ledger, outcomes)
    board = _render_board(approvals, outcomes, args.ledger, ledger_lines)
    args.board.parent.mkdir(parents=True, exist_ok=True)
    args.board.write_text(board, encoding="utf-8")
    for outcome in outcomes:
        report = outcome.report
        nature = "实测" if report.measured else "推演"
        print(
            f"{report.scenario}（{nature}）：{report.total} 份变更，"
            f"放行 {report.released}，拦住 {report.blocked}"
        )
    print(f"\n台账：{args.ledger}（{ledger_lines} 行）")
    print(f"看板：{args.board}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="高风险放行审计 —— 真语料过一遍新闸")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ASES_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="PostgreSQL 连接串（默认读 ASES_DATABASE_URL）",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help=f"台账输出路径（默认 {DEFAULT_LEDGER}）",
    )
    parser.add_argument(
        "--board",
        type=Path,
        default=DEFAULT_BOARD,
        help=f"看板输出路径（默认 {DEFAULT_BOARD}）",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
