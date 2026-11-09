"""改代码用的最小工具 —— **补丁**与 **git**。

设计依据：docs/tasks.md TASK-P1-081（阶段一的执行器是本地补丁器）与
TASK-P1-082（M2 自主闭环用同一个执行器换一份配方）。

**为什么把补丁单独拎出来。** M1 与 M2 的执行器做的事是一样的 —— 按一段
"原文 → 替换后"的说明改文件、提交、报改了哪些文件。**不一样的是那份说明
本身**（M1 修一条，M2 修九条）。把"怎么改"与"改什么"分开，两边就共用同一
份改代码的逻辑，而不是各写一遍 —— 各写一遍的话，两边对"补丁对不上"这件事
的处理就会慢慢分叉，而那种分叉要到某一次补丁真的对不上时才被发现。

**补丁用"整段替换"而不是"行号编辑"。** 行号会漂：文件一改，后面的行号
就不对了，而一个"改错了行号"的补丁会安静地把别的地方改坏。这里记的是
**原文片段**与**替换后的片段** —— 原文不在就报错，绝不"改个大概"。

**``old`` 为空表示"新建这个文件"。** 新文件没有原文可匹配，所以"原文为空"
就是"这个文件还不存在"的约定 —— 而文件真的存在却要新建时，那份补丁会被
拒掉（见 :meth:`Patch.apply`）：一个说不清是"新建"还是"清空内容"的补丁，
不该被默默执行。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Patch:
    """一处改动 —— 改哪个文件、把哪一段换成哪一段。"""

    path: str
    old: str
    new: str

    def apply(self, root: Path) -> bool:
        """把这一处改动落到 ``root`` 下。**原文不在就抛错。**"""
        target = root / self.path
        if not target.exists():
            if self.old:
                raise LookupError(f"{self.path} 不存在 —— 这份补丁却要改它")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.new, encoding="utf-8")
            return True
        if not self.old:
            raise LookupError(f"{self.path} 已经存在 —— 这份补丁却要新建它")
        text = target.read_text(encoding="utf-8")
        if self.old not in text:
            raise LookupError(f"{self.path} 里找不到要替换的那一段 —— 补丁对不上")
        target.write_text(text.replace(self.old, self.new, 1), encoding="utf-8")
        return True


def run_git(root: Path, *args: str) -> str:
    """跑一条 git 命令。**不开 shell** —— 参数是外部输入。

    **不开 shell 不是洁癖。** ``shell=True`` 加上一个由仓库内容拼出来的参数，
    就是一次命令注入 —— 而这一层的参数里有文件名、提交说明、分支名。
    """
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{completed.stderr.strip()}")
    return completed.stdout


__all__ = ["Patch", "run_git"]
