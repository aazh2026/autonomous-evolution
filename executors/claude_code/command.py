"""Claude Code 的命令行 —— 拼一条能跑的 argv。

设计依据：docs/design.md §3.4（执行器协议：CLI）、docs/tasks.md TASK-P1-031。

**这份 argv 是对真实 CLI 实测出来的，不是照文档抄的。** Claude Code 2.1.158
上，有三件事不试就会踩：

1. ``--print`` 配 ``--output-format stream-json`` **必须同时给 ``--verbose``**。
   少了它，CLI 直接以退出码 2 退出并说："When using --print,
   --output-format=stream-json requires --verbose"。所以 ``--verbose`` 不是
   "多打印点东西"，它是这条输出模式的**开关**，少一个整条命令就不成立。
2. ``--session-id`` 必须是**合法的 UUID**。给一个非法值，CLI **不报错** ——
   它会悄悄换成自己生成的那个。于是"我们给的会话 ID"和"CLI 用的会话 ID"
   就对不上了，而且表面看一切正常。
3. ``--session-id`` 会被**原样回显**在每一条事件的 ``session_id`` 字段里。

第 2、3 条合起来就是本适配器**核对会话 ID**的理由：我们自己生成 UUID 交给
CLI，然后核对它报回来的那个是不是同一个。CLI 悄悄换掉我们的 ID 时，这
条核对会立刻炸出来 —— 而不核对的话，别的事件就会被记到这个任务头上。

**为什么权限模式是适配器级的而不是会话级的。** 权限模式对应的是自治级别
L1~L4（design.md §3.5C），而自治级别是**策略层**按任务判定的结果 ——
把它做成会话配置字段，就得让策略层参与每次会话创建；做成适配器级参数，
它至少是一个**显式的、被写在构造处的**选择，而不是藏在某个 dict 里。策略
层接上之后，这里会多一个来源；在那之前，它由部署方在构造适配器时定下。
"""

from __future__ import annotations

from executors.base import ExecutionTask, SessionConfig

#: 非交互模式：跑完就打印结果，不进 REPL。
FLAG_PRINT: str = "--print"
#: 事件流输出格式。
FLAG_OUTPUT_FORMAT: str = "--output-format"
#: 事件流输出格式的值。
OUTPUT_FORMAT_STREAM_JSON: str = "stream-json"
#: **必须**与 ``--print`` + stream-json 同时出现，否则 CLI 以退出码 2 退出。
FLAG_VERBOSE: str = "--verbose"
#: 会话 ID —— 我们给，CLI 回显。
FLAG_SESSION_ID: str = "--session-id"
#: 模型。
FLAG_MODEL: str = "--model"
#: 权限模式。
FLAG_PERMISSION_MODE: str = "--permission-mode"


def build_prompt(task: ExecutionTask) -> str:
    """把任务变成给 CLI 的那段提示词。

    标题与正文都带上：CLI 只看得见这段文字，而标题是**给人看的**任务名 ——
    丢掉它，模型就少了一句上下文。
    """
    title = task.title.strip()
    instruction = task.instruction.strip()
    if not title:
        return instruction
    if not instruction:
        return title
    return f"{title}\n\n{instruction}"


def build_command(
    *,
    binary: str,
    config: SessionConfig,
    task: ExecutionTask,
    session_id: str,
    permission_mode: str = "default",
) -> tuple[str, ...]:
    """这次执行的命令行。

    顺序是固定的：先开关，再会话与模型，最后提示词。提示词放最后且作为
    **单独一个参数**传（不拼进别的串）—— argv 里的每个元素都会被原样送到
    进程，拼接会引入引号与转义的歧义。
    """
    argv: list[str] = [
        binary,
        FLAG_PRINT,
        FLAG_OUTPUT_FORMAT,
        OUTPUT_FORMAT_STREAM_JSON,
        FLAG_VERBOSE,
        FLAG_SESSION_ID,
        session_id,
    ]
    if config.model:
        argv.extend((FLAG_MODEL, config.model))
    if permission_mode:
        argv.extend((FLAG_PERMISSION_MODE, permission_mode))
    argv.append(build_prompt(task))
    return tuple(argv)


__all__ = [
    "FLAG_MODEL",
    "FLAG_OUTPUT_FORMAT",
    "FLAG_PERMISSION_MODE",
    "FLAG_PRINT",
    "FLAG_SESSION_ID",
    "FLAG_VERBOSE",
    "OUTPUT_FORMAT_STREAM_JSON",
    "build_command",
    "build_prompt",
]
