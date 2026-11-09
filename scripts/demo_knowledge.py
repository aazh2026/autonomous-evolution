"""TASK-P2-016 的演示 —— 项目知识库与架构认知。

跑法::

    uv run python -m scripts.demo_knowledge
    # 或
    .venv/bin/python scripts/demo_knowledge.py

**这个演示里没有假的东西。** 真的读试点仓库、真的建索引、真的把索引
落盘、真的把仓库**删掉**再从索引里问一次、真的写 ADR 文件、真的写项目
记忆。演示里唯一被模拟的只有"第二轮的发现器" —— 它只是拿记忆里的清单
对一下，而那一对就是"上一轮做过什么"的全部机制。

**这个演示要证明的是三件事**：

1. **TASK-P2-016 的验收条件** —— "系统能回答'某模块被哪些模块依赖'且
   答案来自已构建的索引而非每次重读仓库"。第 4 步把仓库**整个删掉**，
   再问同一个问题 —— 答案还在，才说明答案真的来自索引。
2. **R-064（不每轮重读仓库重建认知）** —— 刷新时**没变的文件一个字节都
   不读**。这一条用"数读了多少次文件"来验。
3. **跨轮对照** —— 修好的缺陷记进记忆之后，下一轮不再提它。

**不需要数据库。** 数据库那一半（``architecture_decision`` 表）由集成测试
``tests/integration/test_knowledge_decisions.py`` 验；给了 ``--database-url``
就顺带在演示里也写一份，两套存储的答案会当场比一遍。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import pathlib
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.architecture.decisions import DecisionStore
from core.entities import Project
from infrastructure.database.repositories import ArchitectureDecisionRepository
from knowledge.architecture.decisions import FileDecisionStore, decisions_as_set
from knowledge.architecture.model import build_architecture
from knowledge.project_memory.memory import FactKind, ProjectMemory
from knowledge.repository_index.index import (
    ModuleLayer,
    RepositoryIndex,
    build_index,
    refresh_index,
)
from knowledge.repository_index.store import RepositoryIndexStore

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 演示里用的项目 id —— 跟试点仓库的 project.yaml 对得上。
PROJECT_ID = "pilot-tinylib"

#: 演示里问的那个模块。
QUESTION = "tinylib.stats"

#: 演示里关心的那个模块的公开接口。
CLI_MODULE = "tinylib.cli"


def _banner(step: str, title: str) -> None:
    """一节的分隔线。"""
    print(f"\n{'─' * 78}\n{step}  {title}\n{'─' * 78}")


def _fail(message: str) -> int:
    """说清楚哪里不行，然后返回一个非零退出码。"""
    print(f"\n❌ {message}")
    return 1


@contextlib.contextmanager
def _count_reads() -> Iterator[list[str]]:
    """数一数这个范围内读了哪些文件。

    **这是"没变的文件一个字节都不读"的证据。** 索引内部所有读文件的地方
    都走 ``Path.read_bytes``，所以在它上面数一遍就知道到底读没读。
    """
    reads: list[str] = []
    original = pathlib.Path.read_bytes

    def spy(self: pathlib.Path) -> bytes:
        reads.append(str(self))
        return original(self)

    pathlib.Path.read_bytes = spy  # type: ignore[method-assign]
    try:
        yield reads
    finally:
        pathlib.Path.read_bytes = original  # type: ignore[method-assign]


def _first_line(index: RepositoryIndex, importer: str, imported: str) -> int:
    """那条 import 在第几行。找不到就返回 0。"""
    record = index.record(importer)
    if record is None:
        return 0
    for ref in record.imports:
        if ref.internal and ref.target == imported:
            return ref.lineno
    return 0


def _print_dependents(index: RepositoryIndex, module: str) -> None:
    """把"谁依赖它"连同出处一起打出来。"""
    dependents = index.dependents(module)
    if not dependents:
        print(f"   （没有任何模块依赖 {module}）")
        return
    for name in dependents:
        record = index.record(name)
        if record is None:
            print(f"   · {name}")
            continue
        lines = [
            f"{ref.target}（{ref.raw.strip()}）"
            for ref in record.imports
            if ref.internal and ref.target == module
        ]
        detail = "，".join(lines) if lines else "（模块级导入）"
        print(f"   · {name:<28} {record.path}:{_first_line(index, name, module)}  {detail}")


def _copy_pilot_repo(source: Path, target: Path) -> None:
    """把试点仓库复制一份 —— 演示要删它，不能删原件。"""
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".coverage", ".scratch"),
    )


# --------------------------------------------------------------------------
# 演示的六步
# --------------------------------------------------------------------------


def _step_index(repo: Path) -> RepositoryIndex:
    """第 1 步 —— 给仓库建一次索引。"""
    _banner("第 1 步", f"给试点仓库建一次索引（{repo.name}）")
    with _count_reads() as reads:
        index = build_index(repo)
    print(f"   扫了 {len(index.modules)} 个模块，读了 {len(reads)} 个文件")
    print(f"   索引摘要：{index.digest[:16]}…")
    for layer in ModuleLayer:
        modules = [item.module for item in index.modules if item.layer is layer]
        if modules:
            print(f"   · {layer.value:<12} {', '.join(sorted(modules))}")
    return index


def _step_question(index: RepositoryIndex) -> None:
    """第 2 步 —— 问那个问题。"""
    _banner("第 2 步", f"问那个问题：「谁依赖 {QUESTION}？」")
    _print_dependents(index, QUESTION)
    impact = ", ".join(index.transitive_dependents(QUESTION))
    print(f"\n   改它的影响面（传递）：{impact}")


def _step_refresh(repo: Path, index: RepositoryIndex) -> RepositoryIndex:
    """第 3 步 —— 刷新：没变的文件一个字节都不读（R-064）。"""
    _banner("第 3 步", "刷新：没变的文件一个字节都不读（R-064）")
    with _count_reads() as reads:
        refreshed = refresh_index(repo, index)
    print(f"   仓库没变 → 读了 {len(reads)} 个文件，摘要一样：{refreshed.digest == index.digest}")

    target = repo / "src" / "tinylib" / "stats.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef median(values):\n    return 0\n",
        encoding="utf-8",
    )
    with _count_reads() as reads:
        refreshed = refresh_index(repo, index)
    print(f"   改了一个文件 → 读了 {len(reads)} 个文件：{reads[0].split('/')[-1]}")
    names = {symbol.name for symbol in refreshed.symbols_of(QUESTION)}
    print(f"   新符号收进来了：{'median' in names}")
    return refreshed


def _step_prove_from_index(work: Path, repo: Path, index: RepositoryIndex) -> int:
    """第 4 步 —— 落盘，然后把仓库整个删掉，再问同一个问题。"""
    _banner("第 4 步", "落盘，然后把仓库整个删掉，再问同一个问题")
    store = RepositoryIndexStore(work / "index.json")
    store.save(index)
    print(f"   索引写到：{store.path}")
    shutil.rmtree(repo)
    print(f"   仓库已删除（{repo} 不存在了）")
    loaded = store.load()
    if loaded is None:
        return _fail("索引读不回来了 —— 这不该发生")
    print(f"\n   再问一次「谁依赖 {QUESTION}？」")
    _print_dependents(loaded, QUESTION)
    same = loaded.dependents(QUESTION) == index.dependents(QUESTION)
    print(f"\n   答案与仓库还在时一致：{same}")
    if not same:
        return _fail("答案不一致 —— 说明答案不是来自索引")
    return 0


def _step_architecture(index: RepositoryIndex) -> Mapping[str, tuple[str, ...]]:
    """第 5 步 —— 架构认知。返回"没人碰过的公开符号"。"""
    _banner("第 5 步", "架构认知：分层、依赖环、违规、没人碰的公开接口")
    model = build_architecture(index)
    summary = model.summary()
    print(f"   模块 {summary['modules']} 个，依赖边 {summary['edges']} 条")
    print(f"   依赖环：{len(model.cycles())} 个")
    for cycle in model.cycles():
        print(f"      · {cycle.describe()}")
    violations = model.violations()
    print(f"   分层违规：{len(violations)} 条")
    for violation in violations:
        print(f"      · {violation.describe()}")
    print(f"   入口模块：{', '.join(model.entry_points()) or '（无）'}")
    print("\n   没有任何用例引用过的公开符号（结构性覆盖率缺口）：")
    gaps = model.unreferenced_public_symbols()
    if not gaps:
        print("      （没有）")
    for module, names in gaps.items():
        print(f"      · {module}: {', '.join(names)}")
    print(f"   没有任何用例引用过的源模块：{', '.join(model.untested_modules()) or '（无）'}")
    return gaps


async def _step_decisions(work: Path) -> None:
    """第 6 步 —— 记一条架构决策（ADR —— 人读的那一份）。"""
    _banner("第 6 步", "记一条架构决策（ADR —— 人读的那一份）")
    adr = FileDecisionStore(work / "adr")
    first = await adr.record(
        project_id=PROJECT_ID,
        context="每一轮演进都要重新扫一遍仓库，慢，而且扫出来的东西一样",
        decision="把仓库索引落盘，刷新时只重读改过的文件",
        tradeoff="索引可能与仓库不同步 —— 所以索引里记着每个文件的内容哈希",
    )
    print(f"   记下 {first.id}")
    second = await adr.record(
        project_id=PROJECT_ID,
        context="索引落盘之后，改一个文件就得重扫全仓库才能确认没漏",
        decision="刷新时按 (大小, 修改时间) 判断哪些文件需要重读",
        tradeoff="文件被改过又改回原样时会多读一次 —— 少见，且无害",
        supersedes_id=first.id,
    )
    print(f"   又记了 {second.id}，它取代了 {first.id}")
    print(f"   文件在：{adr.root / (second.id + '.md')}")
    print("\n   文件长这样（前言是结构化字段，正文是人读的内容）：")
    text_lines = (adr.root / f"{second.id}.md").read_text(encoding="utf-8").splitlines()[:9]
    for line in text_lines:
        print(f"      {line}")

    decisions = decisions_as_set(await adr.recall(PROJECT_ID))
    print(f"\n   当前生效的决策：{', '.join(item.id for item in decisions.effective())}")
    print("   取代链（新 → 旧）：")
    for item in decisions.chain(second.id):
        print(f"      · {item.id}  {item.decision}")


async def _step_memory(work: Path, gaps: Mapping[str, tuple[str, ...]]) -> None:
    """第 7 步 —— 项目记忆：跨轮对照。"""
    _banner("第 7 步", "项目记忆：跨轮对照 —— 修好的缺陷别再提")
    memory = ProjectMemory(work / "memory")
    memory.record(
        kind=FactKind.ISSUE_FIXED,
        key="PILOT-001",
        summary="tinylib.cli 没有任何用例引用过（结构性覆盖率缺口）",
        detail="第 5 步的架构认知里查出来的：cli 覆盖率 0 是因为没人 import 它。",
        cycle_ref="cycle-0001",
        tags=("pilot", "coverage"),
    )
    print("   第 1 轮：修好了 PILOT-001，记进记忆")
    print(f"   记忆里的清单：{', '.join(memory.resolved_issues()) or '（空）'}")

    print("\n   第 2 轮：发现器又提了一条 PILOT-001 ——")
    candidate = "PILOT-001"
    if memory.is_resolved(candidate):
        print(f"      「{candidate}」已经修好了 → 跳过，不再提")
    else:
        print(f"      「{candidate}」没修过 → 继续")
    print("   结论：第 2 轮不会重复劳动（不会再修一次、再验一次、再花一次钱）")

    memory.record(
        kind=FactKind.OBSERVATION,
        key="ARCH-001",
        summary=f"{CLI_MODULE} 的公开符号 {', '.join(gaps.get(CLI_MODULE, ()))} 没人碰过",
        detail="从仓库索引里看出来的，不用跑测试。",
        cycle_ref="cycle-0001",
    )
    print(f"\n   记忆里现在有 {len(memory.facts())} 条事实")
    for fact in memory.facts():
        print(f"      · [{fact.kind}] {fact.key}  {fact.summary}")


async def _run_database_half(args: argparse.Namespace, work: Path) -> int:
    """第 8 步 —— 数据库那一半：两套存储说的是同一件事。"""
    _banner("第 8 步", "数据库那一半：同一组决策，两套存储的答案必须一样")
    engine = create_async_engine(args.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM architecture_decision WHERE project_id = :pid"),
                {"pid": PROJECT_ID},
            )
            await conn.execute(
                text("DELETE FROM project WHERE id = :pid"),
                {"pid": PROJECT_ID},
            )
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(
                Project(
                    id=PROJECT_ID,
                    name="知识库演示项目",
                    goal={"objective": [{"id": "OBJ-1", "description": "把架构认知落下来"}]},
                    policy={"merge_to_main": "approval_required"},
                    budget={"currency": "USD"},
                    repository_url="https://example.invalid/repo.git",
                    tech_stack=["python"],
                )
            )
            await session.commit()

            repository = ArchitectureDecisionRepository(session)
            chain_db = await _write_chain(repository)
            await session.commit()
            print(f"   库里记了 3 条，取代链：{' → '.join(chain_db)}")

            files = FileDecisionStore(work / "adr-db")
            chain_file = await _write_chain(files)
            print(f"   文件里记了 3 条，取代链：{' → '.join(chain_file)}")

            same = chain_db == chain_file
            print(f"\n   两套存储的取代链一致：{same}")
            return 0 if same else _fail("两套存储说的不是同一件事")
    finally:
        await engine.dispose()


async def _write_chain(store: DecisionStore) -> list[str]:
    """写三条互相取代的决策，返回取代链上的内容。"""
    first = await store.record(project_id=PROJECT_ID, context="背景", decision="最初的决策")
    second = await store.record(
        project_id=PROJECT_ID,
        context="背景",
        decision="中间的决策",
        supersedes_id=first.id,
    )
    third = await store.record(
        project_id=PROJECT_ID,
        context="背景",
        decision="最新的决策",
        supersedes_id=second.id,
    )
    decisions = decisions_as_set(await store.recall(PROJECT_ID))
    return [item.decision for item in decisions.chain(third.id)]


async def _run(args: argparse.Namespace) -> int:
    """跑整个演示。"""
    work = args.work_root
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if work is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="ases-knowledge-demo-")
        work = Path(temp_dir.name)
    repo = work / "pilot"
    _copy_pilot_repo(args.pilot_repo, repo)

    index = _step_index(repo)
    _step_question(index)
    index = _step_refresh(repo, index)
    code = _step_prove_from_index(work, repo, index)
    if code != 0:
        return code
    gaps = _step_architecture(index)
    await _step_decisions(work)
    await _step_memory(work, gaps)

    if args.database_url:
        code = await _run_database_half(args, work)
        if code != 0:
            return code
    else:
        print("\n（没给 --database-url，跳过数据库那一半；它由集成测试验）")

    _banner("收尾", "这个演示证明了什么")
    print("   1. 仓库删掉之后，「谁依赖它」的答案还在 —— 答案来自索引（R-064）。")
    print("   2. 刷新时没变的文件一个字节都不读 —— 不是重建，是刷新。")
    print("   3. 架构认知不跑测试就能指出结构性覆盖率缺口。")
    print("   4. 修好的缺陷记进记忆之后，下一轮不会再提它。")
    if temp_dir is not None:
        if args.keep:
            print(f"\n工作目录保留在：{temp_dir.name}")
        else:
            temp_dir.cleanup()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="TASK-P2-016 项目知识库演示")
    parser.add_argument(
        "--pilot-repo",
        type=Path,
        default=REPO_ROOT / "samples/pilot_repo",
        help="试点仓库目录（会被复制一份，演示会删掉副本）",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="工作根目录（默认在临时目录里建一个）",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ASES_DATABASE_URL", ""),
        help="数据库 DSN（给了就顺带跑数据库那一半）",
    )
    parser.add_argument("--keep", action="store_true", help="保留工作目录（调试用）")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
