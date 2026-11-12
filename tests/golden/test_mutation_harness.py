"""变异验证脚本**自己**的验证。

**为什么一个验证脚本也要被验证。** 这个项目的主张是"一个从没失败过的断言
证明不了任何事"。**同一条主张对验证脚本本身成立** —— 如果
``scripts/verify_golden_mutants.py`` 无论什么情况都报"抓住 11/11"，那
"11/11"这三个字什么也不证明，而且它还会**挡住**真正的问题：它会让所有人
以为金标有牙。

所以这里验的是**判定逻辑**（:func:`scripts.verify_golden_mutants._decide`），
也就是脚本从证据得出结论的那一步。那一步是纯函数 —— 不碰工作树、不跑
pytest —— 所以这几条测试是毫秒级的，不需要建工作树。

**验的是四条会骗人的路径。**

1. **目标本来就红。** 变异前就是红的，变异之后还是红的 —— 脚本要是把它
   算成"抓住了"，那是在发一张假通行证。
2. **目标压根没红。** 变异生效了、测试也跑了、但没抓住 —— 这才是"没抓住"。
3. **只红了一条。** 声明了两条证人，变异之后只红了一条 —— 这是**半个
   真相**，不能算成抓住。
4. **参数化用例的编号带后缀。** pytest 报出来的是
   ``test_foo[workspace_id]``，声明的是 ``test_foo`` —— 对不上就会被读成
   "没红"，**方向正好反了**：明明抓住了，脚本说没抓住。

**还验一条最容易出错的：** 红目标的节点 id 是不是真的存在。它必须是
**真的能被 pytest 收集到** —— 目标写错了，脚本会报"没变红"，而那跟"规则
没人守"是同一件事，只是更隐蔽。

跑法::

    uv run pytest tests/golden/test_mutation_harness.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from scripts.verify_golden_mutants import (
    BASELINE_RED,
    CAUGHT,
    COLLECTION_ERROR,
    MISSED,
    Run,
    _decide,
    _matches,
    _module_name,
    _red_nodes,
)
from tests.golden.matrix import CASES, REPO_ROOT, FailClosedCase, Mutant

#: 一条假的红目标。
_TARGET_A: str = "tests/unit/example/test_thing.py::TestThing::test_one"
_TARGET_B: str = "tests/unit/example/test_thing.py::TestThing::test_two"


def _mutant() -> Mutant:
    """一个假的变异体。"""
    return Mutant(
        id="MUT-TEST-01",
        path="core/example.py",
        before="原来",
        after="改坏",
        why="测试用",
    )


def _case(targets: Sequence[str]) -> FailClosedCase:
    """一行假的矩阵行 —— 只需要 ``red_targets``。"""

    async def _observe(_workspace: Path) -> Mapping[str, object]:
        return {}

    return FailClosedCase(
        id="FC-TEST",
        fault="测试用",
        invariant="测试用",
        covers=(),
        observe=_observe,
        expect={},
        mutants=(),
        red_targets=tuple(targets),
    )


def _red_output(node_ids: Sequence[str]) -> str:
    """造一份 pytest 的失败摘要输出。"""
    lines = ["=========================== short test summary info ============================"]
    lines.extend(f"FAILED {node} - AssertionError: 故意红的" for node in node_ids)
    return "\n".join(lines)


def _green() -> Run:
    """一次什么都没红的跑。"""
    return Run(0, "1 passed in 0.01s")


class TestTheHarnessCanTellTheTruth:
    """判定逻辑：证据 → 结论。"""

    def test_everything_red_is_caught(self) -> None:
        outcome = _decide(
            _mutant(),
            _case([_TARGET_A, _TARGET_B]),
            _green(),
            Run(1, _red_output([_TARGET_A, _TARGET_B])),
        )
        assert outcome.status == CAUGHT
        assert outcome.caught

    def test_a_target_that_was_already_red_is_not_a_catch(self) -> None:
        """**基线就红的话，"抓住了"不作数。**

        这是最容易发假通行证的一条：目标本来就红（写错了、或者那条分支从来
        没跑过），变异之后它还是红，脚本要是报"抓住了"，那是在替一条
        没人守的规则发通行证。
        """
        outcome = _decide(
            _mutant(),
            _case([_TARGET_A]),
            Run(1, _red_output([_TARGET_A])),
            Run(1, _red_output([_TARGET_A])),
        )
        assert outcome.status == BASELINE_RED
        assert not outcome.caught

    def test_nothing_red_is_missed(self) -> None:
        outcome = _decide(
            _mutant(),
            _case([_TARGET_A, _TARGET_B]),
            _green(),
            _green(),
        )
        assert outcome.status == MISSED
        assert not outcome.caught

    def test_only_one_of_two_red_is_missed(self) -> None:
        """**半个真相不算抓住。**

        声明了两条证人，变异之后只红了一条 —— 那说明另一条**没有牙齿**。
        报成"抓住"就把这条缺口盖住了。
        """
        outcome = _decide(
            _mutant(),
            _case([_TARGET_A, _TARGET_B]),
            _green(),
            Run(1, _red_output([_TARGET_A])),
        )
        assert outcome.status == MISSED
        assert not outcome.caught
        assert _TARGET_B in outcome.detail

    def test_a_broken_collection_is_not_a_catch(self) -> None:
        """变异把代码改到收集不起来 —— 那是变异体本身写坏了。"""
        outcome = _decide(
            _mutant(),
            _case([_TARGET_A]),
            _green(),
            Run(2, "ERRORS\n=======\nERROR tests/unit/example/test_thing.py\nSyntaxError"),
        )
        assert outcome.status == COLLECTION_ERROR
        assert not outcome.caught


class TestTheNodeIdBookkeeping:
    """节点 id 的账 —— 这里算错，结论的方向就反了。"""

    def test_a_parametrised_case_matches_its_plain_name(self) -> None:
        """pytest 报的是 ``test_foo[workspace_id]``，声明的是 ``test_foo``。"""
        assert _matches(_TARGET_A, _TARGET_A)
        assert _matches(_TARGET_A, f"{_TARGET_A}[workspace_id]")
        assert not _matches(_TARGET_A, _TARGET_B)
        assert not _matches(_TARGET_A, f"{_TARGET_A}extra")

    def test_a_parametrised_case_is_counted_as_red(self) -> None:
        """**这条曾经真的错过一次。**

        参数化的那条授权测试报了 ``...[workspace_id]``，而声明里写的是函数名
        —— 脚本于是说"没抓住"。**明明抓住了，说成没抓住**：方向反了，而且
        会让人去改一条根本没坏的规则。
        """
        declared = "tests/unit/harness/security/test_authorization.py::TestTheModelGuardsItself"
        "::test_an_empty_identity_field_is_refused"
        outcome = _decide(
            _mutant(),
            _case([declared]),
            _green(),
            Run(1, _red_output([f"{declared}[workspace_id]"])),
        )
        assert outcome.status == CAUGHT

    def test_failed_lines_are_picked_out(self) -> None:
        nodes = _red_nodes(_red_output([_TARGET_A, _TARGET_B]))
        assert nodes == {_TARGET_A, _TARGET_B}

    def test_a_path_becomes_a_module_name(self) -> None:
        assert _module_name("harness/security/authorization.py") == (
            "harness.security.authorization"
        )
        assert _module_name("core/evolution/__init__.py") == "core.evolution"


#: 收集用的 pytest 插件 —— 把收集到的用例编号导出来。
#:
#: **为什么需要它。** pytest 9 的 ``--collect-only -q`` 不再逐条列用例编号了
#: （只给"哪个文件几个"），而 ``-v`` 给的是树。**要一份平的编号清单，只能
#: 自己挂个钩子。**
_PLUGIN_SOURCE: str = """
import json


def pytest_addoption(parser):
    parser.addoption("--dump-node-ids", action="store", default=None)


def pytest_collection_finish(session):
    destination = session.config.getoption("--dump-node-ids")
    if not destination:
        return
    ids = [item.nodeid for item in session.items]
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(ids, handle)
"""


@pytest.fixture(scope="module")
def collected(tmp_path_factory: pytest.TempPathFactory) -> tuple[set[str], str]:
    """一次把全部红目标收集一遍。

    :returns: (收集到的编号, pytest 的输出)
    """
    directory = tmp_path_factory.mktemp("collect")
    plugin = directory / "dump_node_ids.py"
    plugin.write_text(_PLUGIN_SOURCE, encoding="utf-8")
    dump = directory / "node_ids.json"

    targets = sorted({target for case in CASES for target in case.red_targets})
    environment = dict(os.environ)
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(directory), *([inherited] if inherited else [])]
    )
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *targets,
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "dump_node_ids",
            f"--dump-node-ids={dump}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    output = (done.stdout or "") + (done.stderr or "")
    if not dump.exists():
        return set(), output
    return set(json.loads(dump.read_text(encoding="utf-8"))), output


class TestTheRedTargetsReallyExist:
    """红目标必须**真的能被 pytest 收集到**。"""

    def test_every_red_target_is_collectable(self, collected: tuple[set[str], str]) -> None:
        found, output = collected
        missing: list[str] = []
        for case in CASES:
            for target in case.red_targets:
                hit = any(seen == target or seen.startswith(f"{target}[") for seen in found)
                if not hit:
                    missing.append(f"{case.id} → {target}")
        assert not missing, (
            "这些红目标 pytest 收不到 —— 目标 id 写错了，或者那条用例被改名了：\n"
            + "\n".join(f"  {item}" for item in missing)
            + "\n\npytest 的输出：\n"
            + output[-3000:]
        )
