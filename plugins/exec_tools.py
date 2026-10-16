"""
命令执行工具组。

这两个工具是 dangerous 的 —— 控制台在 `approve` 模式下会在执行前询问用户，
这是比"黑名单"更可靠的一道防线（黑名单只能拦已知模式）。

关于取消：底层用 sandbox.run_process（Popen + 短轮询）而不是 subprocess.run，
用户按 Ctrl+C 时令牌触发 → 子进程整棵树被杀 → 这里拿到 was_cancelled=True，
返回 [cancelled] 让模型知道「这条命令没跑完」，而不是静默丢掉。
"""

from __future__ import annotations

import sys

from core import check_command, run_process, tool, workspace

PLUGIN_INFO = {"name": "命令执行", "version": "1.1", "author": "mini-agent"}


@tool(
    "run_shell",
    "执行一条 shell 命令并返回标准输出/错误。用于查看环境、跑构建、调用命令行工具。",
    params={
        "command": {"type": "string", "description": "要执行的命令"},
        "timeout": {"type": "integer", "description": "超时秒数", "default": 120},
    },
    required=["command"],
    dangerous=True,
)
def run_shell(command: str, timeout: int = 120) -> str:
    reason = check_command(command)
    if reason:
        return f"[blocked] {reason}"

    try:
        code, out, err, cancelled = run_process(
            command, shell=True, cwd=workspace(), timeout=timeout,
        )
    except TimeoutError:
        return f"[error] 命令超时（{timeout}s）：{command}"
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"

    if cancelled:
        return f"[cancelled] 命令已被用户终止（进程树已杀掉）：{command}"
    return _render(code, command, out, err)


@tool(
    "run_python",
    "执行一段 Python 代码并返回输出。用于解析二进制文件、做统计、装库等确定性计算。",
    params={
        "code": {"type": "string", "description": "要执行的 Python 源码"},
        "timeout": {"type": "integer", "description": "超时秒数", "default": 120},
    },
    required=["code"],
    dangerous=True,
)
def run_python(code: str, timeout: int = 120) -> str:
    try:
        code_, out, err, cancelled = run_process(
            [sys.executable, "-c", code], cwd=workspace(), timeout=timeout,
        )
    except TimeoutError:
        return f"[error] Python 执行超时（{timeout}s）"
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"

    if cancelled:
        return "[cancelled] Python 执行已被用户终止"
    return _render(code_, None, out, err)


def _render(code: int, command: str | None, out: str, err: str) -> str:
    parts = [f"[exit={code}] {command}" if command else f"[exit={code}]"]
    if out:
        parts.append(out)
    if err:
        parts.append("--- stderr ---\n" + err)
    if not out and not err:
        parts.append("(无输出)")
    return "\n".join(parts)
