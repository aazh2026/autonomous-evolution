"""负金标矩阵 —— 失败关闭的每一条，逐条钉住。

**这个文件验的是什么。** :mod:`tests.golden.matrix` 里那张表。每行注入一个
真实故障，然后断言系统的反应是**失败关闭**的 —— "不知道"、"没量过"、"崩了"
一律不等于"没问题"。

**为什么这些断言值得单独存在。** 因为 fail-open 的错**在顺利路径上根本
看不见**。顺利的时候没人会注意到"检查压根没启动"被当成了"通过"；所以矩阵
里一半的行是故意把系统推进故障状态的。

**矩阵自己也受检。** :class:`TestTheMatrixItself` 查的是矩阵**作为数据**
的完整性 —— 尤其是"变异体的 ``before`` 片段在目标文件里唯一命中"这一条。
这一条是整个变异验证的地基：片段对不上时，变异脚本会报告"没生效"，而一个
从没生效过的变异体如果被当成"抓住了"，就等于给自己发了一张假通行证。

跑法：

```bash
uv run pytest tests/golden/test_fail_closed_matrix.py -v
```

**证明它们有牙**（把每个变异体真的改坏一次，看这一行会不会红）：

```bash
uv run python -m scripts.verify_golden_mutants
```
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.golden.matrix import CASES, REPO_ROOT, FailClosedCase, all_mutants


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个真的存在的工作区。

    **它必须真的存在。** :class:`harness.verification.engine.VerificationRequest`
    在构造时就检查工作区是不是目录 —— 契约要求工作区存在，这条要求本身
    也是被验的东西之一，不能在测试里绕过去。
    """
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestTheFailClosedMatrix:
    """每一行一条用例。"""

    @pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
    async def test_row(self, case: FailClosedCase, workspace: Path) -> None:
        """注入故障，断言反应是失败关闭的。

        **比的是整组事实，不是某一个字段。** 只断言"结论是 error"会漏掉
        "结论对了但顺带把别的东西也放过了"；整组事实一起比，才看得出这次
        到底发生了什么。
        """
        observed = dict(await case.observe(workspace))
        expected = dict(case.expect)

        assert observed == expected, (
            f"{case.id}：{case.fault}\n"
            f"  守住的不变量：{case.invariant}\n"
            f"  实际观测：{observed}\n"
            f"  期望：    {expected}"
        )


class TestTheMatrixItself:
    """矩阵作为数据，它自己得是干净的。

    **这一组不是自欺欺人的形式检查。** 变异验证脚本完全依赖矩阵里的
    ``before`` 片段 —— 片段对不上，脚本就会"报告抓住 11/11"而其实有一个
    压根没写进文件。那种失败模式是**静默的**：脚本说全绿，规则其实没人
    守。所以片段的唯一性在这里被查。
    """

    def test_every_row_has_a_unique_id(self) -> None:
        ids = [case.id for case in CASES]
        assert len(ids) == len(set(ids)), "矩阵里有重复的行号"

    def test_every_row_has_at_least_one_mutant(self) -> None:
        """**一行没有变异体，就是一条没人证明过会失败的断言。**"""
        for case in CASES:
            assert case.mutants, f"{case.id} 没有专属变异体 —— 它凭什么说自己有牙"

    def test_every_row_has_at_least_one_red_target(self) -> None:
        for case in CASES:
            assert case.red_targets, f"{case.id} 没有声明「变异后必须变红」的目标"

    def test_every_mutant_belongs_to_exactly_one_row(self) -> None:
        """一个变异体挂两行，等于其中一行没有专属变异体。"""
        owners: dict[str, list[str]] = {}
        for case in CASES:
            for mutant in case.mutants:
                owners.setdefault(mutant.id, []).append(case.id)
        for mutant_id, case_ids in owners.items():
            assert len(case_ids) == 1, (
                f"变异体 {mutant_id} 同时挂在 {case_ids} 上 —— 每行只该有一个专属变异体"
            )

    def test_every_mutant_is_referenced(self) -> None:
        """所有变异体都被引用 —— 没有游离的变异体。"""
        referenced = {mutant.id for case in CASES for mutant in case.mutants}
        for mutant in all_mutants():
            assert mutant.id in referenced, f"变异体 {mutant.id} 没有被任何行引用"

    def test_every_mutant_fragment_hits_exactly_once(self) -> None:
        """**变异体的 ``before`` 必须在目标文件里唯一命中。**

        这是整套变异验证的地基。片段找不到（文件改了、缩进变了、代码挪了）
        或者命中多处（片段不唯一），变异就等于没发生 —— 而脚本会把"没发生"
        报成"抓住了"。**那是一张假通行证，比没有通行证更坏。**

        片段对不上时给出的提示是"脚本过期了"，不是"测试失败" —— 这两件事
        的处置完全不同：前者要更新脚本，后者要修代码。
        """
        for mutant in all_mutants():
            target = REPO_ROOT / mutant.path
            assert target.exists(), f"变异体 {mutant.id} 的目标文件不存在：{mutant.path}"
            source = target.read_text(encoding="utf-8")
            hits = source.count(mutant.before)
            assert hits == 1, (
                f"变异体 {mutant.id} 的 before 片段在 {mutant.path} 里命中 {hits} 次 —— "
                f"必须恰好一次。片段对不上意味着脚本过期了："
                f"要么代码挪了位，要么这段源码已经改过。"
            )

    def test_every_mutant_changes_something(self) -> None:
        """``before`` 与 ``after`` 不能一样 —— 那叫等价变异，什么也证明不了。"""
        for mutant in all_mutants():
            assert mutant.before != mutant.after, f"变异体 {mutant.id} 是等价变异"
            assert mutant.why.strip(), f"变异体 {mutant.id} 没写它改坏了什么"

    def test_every_red_target_points_at_a_real_file(self) -> None:
        """``red_targets`` 指的文件必须真的存在。

        目标写错了，变异验证脚本会报"没变红" —— 而那跟"规则没人守"是同
        一件事，只是更隐蔽。
        """
        for case in CASES:
            for target in case.red_targets:
                path_part = target.split("::", 1)[0]
                assert (REPO_ROOT / path_part).exists(), (
                    f"{case.id} 的红目标 {target} 指向的文件不存在：{path_part}"
                )
