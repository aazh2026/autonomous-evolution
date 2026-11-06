"""打桩 CLI —— 让 CLI 执行器能在单元测试里被真的跑起来。

**为什么需要它。** CLI 执行器的绝大部分逻辑在**进程边界**上：起进程、读
stdout 的每一行、把退出码变成统一状态、在进程还在跑的时候把它掐掉。这些
逻辑用假对象测不出来 —— 假对象不会真的产生 NDJSON，也不会真的以退出码 1
结束，更不会真的在被 SIGKILL 之后停下。

所以这里放一个**真的可执行文件**：一个几十行的 Python 脚本，按一份 JSON
配置吐事件、记下自己的 argv、然后按配置的退出码退出。它是"真的"（真的是
一个子进程），但它**不调用任何真实 Agent** —— 不联网、不花钱、不碰工作
空间里的任何文件。

**配置与脚本放在一起**，名字只差扩展名：``stub_cli.py`` 与 ``stub_cli.json``。
这样测试只要准备一个目录，剩下的由 :func:`write_stub` 安排。

**它记下自己的 argv 与环境**，是为了让"凭证只走环境变量、不进 argv"这条
约定**可断言** —— 那条约定一旦破了，从结果里是看不出来的。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 打桩 CLI 的脚本正文。
#:
#: 用原始字符串（``r"""``）写，是因为里面要保留 ``\\n`` 这类转义 —— 那是
#: **生成出来的脚本**要用的，不是这段 Python 自己要解释的。
_STUB_SOURCE = r'''#!/usr/bin/env python3
"""打桩 CLI —— 按配置吐事件、记下 argv、按配置的退出码退出。"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).with_suffix(".json")
LOG_PATH = Path(__file__).with_suffix(".log")

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

ARGV = sys.argv[1:]

SESSION_ID = None
FLAG = CONFIG.get("session_id_flag", "--session-id")
if FLAG in ARGV:
    INDEX = ARGV.index(FLAG)
    if INDEX + 1 < len(ARGV):
        SESSION_ID = ARGV[INDEX + 1]

# 报告用的会话 ID。默认是 argv 里拿到的那个；配了别的值就报别的 ——
# 用来模拟"CLI 悄悄换掉了我们给的会话 ID"。
REPORTED_SESSION_ID = CONFIG.get("reported_session_id") or SESSION_ID or ""

if CONFIG.get("record", True):
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "argv": ARGV,
                    "cwd": os.getcwd(),
                    "session_id": SESSION_ID,
                    "env": {
                        key: os.environ.get(key)
                        for key in CONFIG.get("recorded_env", [])
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

for line in CONFIG.get("raw_lines", []):
    print(line, flush=True)

for event in CONFIG.get("events", []):
    line = json.dumps(event, ensure_ascii=False)
    line = line.replace("__SESSION_ID__", REPORTED_SESSION_ID)
    print(line, flush=True)

STDERR = CONFIG.get("stderr", "")
if STDERR:
    print(STDERR, file=sys.stderr, flush=True)

DELAY = float(CONFIG.get("sleep_before_exit", 0.0))
if DELAY > 0:
    time.sleep(DELAY)

sys.exit(int(CONFIG.get("exit_code", 0)))
'''

#: 事件里代表"会话 ID"的位置占位符。脚本会把它换成 argv 里拿到的真值。
SESSION_PLACEHOLDER: str = "__SESSION_ID__"


def write_stub(
    directory: Path,
    *,
    events: list[Any],
    exit_code: int = 0,
    sleep_before_exit: float = 0.0,
    stderr: str = "",
    raw_lines: tuple[str, ...] = (),
    session_id_flag: str = "--session-id",
    reported_session_id: str | None = None,
    recorded_env: tuple[str, ...] = (),
    record: bool = True,
) -> Path:
    """在 ``directory`` 里放一个打桩 CLI，返回它的可执行文件路径。

    :param events: 逐条吐出的事件（会被 ``json.dumps`` 序列化成一行）。
    :param raw_lines: 原样吐出的行 —— 用来造"根本不是 JSON"的坏输出。
    :param reported_session_id: 让打桩 CLI 报**另一个**会话 ID —— 用来模拟
        "CLI 悄悄换掉了我们给的会话 ID"。
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "stub_cli.py"
    script.write_text(_STUB_SOURCE, encoding="utf-8")
    script.chmod(0o755)
    config = {
        "events": list(events),
        "exit_code": exit_code,
        "sleep_before_exit": sleep_before_exit,
        "stderr": stderr,
        "raw_lines": list(raw_lines),
        "session_id_flag": session_id_flag,
        "recorded_env": list(recorded_env),
        "record": record,
    }
    if reported_session_id is not None:
        config["reported_session_id"] = reported_session_id
    (directory / "stub_cli.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return script


def read_log(directory: Path) -> list[dict[str, Any]]:
    """读打桩 CLI 记下的每一次调用。"""
    log = directory / "stub_cli.log"
    if not log.exists():
        return []
    records = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def claude_events(
    *,
    is_error: bool = False,
    api_error_status: int | None = None,
    cost_usd: float = 0.01,
    duration_ms: int = 120,
    tool_calls: int = 1,
    session_id: str = SESSION_PLACEHOLDER,
) -> list[dict[str, Any]]:
    """一份形状与真实 Claude Code 一致的 NDJSON 事件流。

    形状照着 Claude Code 2.1.158 的实测输出写：``system/init`` →
    ``assistant``（文字 + 工具调用）→ ``result``。改动这里的字段名要连带
    改 :mod:`executors.claude_code.events` 的映射表 —— 两边一起改，才不会
    出现"测试与实现一起错"的情况。
    """
    assistant_blocks: list[dict[str, Any]] = [{"type": "text", "text": "好的"}]
    for index in range(tool_calls):
        assistant_blocks.append(
            {
                "type": "tool_use",
                "id": f"toolu_{index:04d}",
                "name": "Read",
                "input": {"file_path": "README.md"},
            }
        )
    events: list[dict[str, Any]] = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "cwd": "/tmp",
            "model": "test-model",
            "permissionMode": "default",
            "claude_code_version": "0.0.0-stub",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": assistant_blocks},
            "session_id": session_id,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "api_error_status": api_error_status,
            "duration_ms": duration_ms,
            "num_turns": 1,
            "result": "好的",
            "stop_reason": "end_turn",
            "session_id": session_id,
            "total_cost_usd": cost_usd,
        },
    ]
    return events


__all__ = [
    "SESSION_PLACEHOLDER",
    "claude_events",
    "read_log",
    "write_stub",
]
