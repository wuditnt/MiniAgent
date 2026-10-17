"""
系统操作增强工具组。

kill_process    按 PID 杀进程
wait_for_file   等文件出现（带超时）
clipboard_read  读剪贴板
clipboard_write 写剪贴板
"""

from __future__ import annotations

import os
import sys
import time

from core import check_command, read_path, run_process, tool

PLUGIN_INFO = {"name": "系统增强", "version": "1.0", "author": "mini-agent"}


@tool(
    "kill_process",
    "按 PID 终止进程。",
    params={
        "pid": {"type": "integer", "description": "进程 PID"},
        "force": {"type": "boolean", "description": "强制终止（Windows 用 /F）", "default": False},
    },
    required=["pid"],
    dangerous=True,
)
def kill_process(pid: int, force: bool = False) -> str:
    if sys.platform == "win32":
        flag = " /F" if force else ""
        cmd = check_command(f"taskkill /PID {pid}{flag}")
    else:
        flag = "-9 " if force else ""
        cmd = check_command(f"kill {flag}{pid}")
    code, out, err, _ = run_process(cmd, shell=True, timeout=10)
    if code != 0:
        return f"[error] 终止失败：{err or out}"
    return f"[ok] 已终止进程 {pid}\n{out}"


@tool(
    "wait_for_file",
    "等待某个文件/目录出现，常用于等另一个进程把文件写好。",
    params={
        "path": {"type": "string", "description": "要等待的文件路径"},
        "timeout": {"type": "integer", "description": "最长等待秒数", "default": 60},
        "interval": {"type": "number", "description": "轮询间隔秒数", "default": 0.5},
    },
    required=["path"],
)
def wait_for_file(path: str, timeout: int = 60, interval: float = 0.5) -> str:
    ap = read_path(path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(ap):
            return f"[ok] 文件已出现：{ap}"
        time.sleep(interval)
    return f"[error] 等待 {timeout} 秒后文件仍未出现：{ap}"


@tool(
    "clipboard_read",
    "读取系统剪贴板内容。",
)
def clipboard_read() -> str:
    try:
        import pyperclip  # type: ignore

        return pyperclip.paste()
    except ImportError:
        pass
    # 退回系统命令
    if sys.platform == "win32":
        code, out, err, _ = run_process("powershell -Command Get-Clipboard", shell=True, timeout=5)
    elif sys.platform == "darwin":
        code, out, err, _ = run_process("pbpaste", shell=True, timeout=5)
    else:
        code, out, err, _ = run_process("xclip -selection clipboard -o", shell=True, timeout=5)
    if code != 0:
        return f"[error] 读取剪贴板失败：{err or out}\n[提示] pip install pyperclip 可获得更好的兼容性"
    return out


@tool(
    "clipboard_write",
    "把文本写入系统剪贴板。",
    params={"text": {"type": "string", "description": "要写入的文本"}},
    required=["text"],
    dangerous=True,
)
def clipboard_write(text: str) -> str:
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return f"[ok] 已写入剪贴板（{len(text)} 字符）"
    except ImportError:
        pass
    if sys.platform == "win32":
        # 用 clip 命令；注意结尾会加换行
        cmd = check_command("clip")
        from subprocess import Popen, PIPE

        p = Popen(cmd, stdin=PIPE, shell=True)
        p.communicate(text.encode("utf-8"))
        if p.returncode != 0:
            return f"[error] 写入剪贴板失败"
        return f"[ok] 已写入剪贴板（{len(text)} 字符）"
    elif sys.platform == "darwin":
        from subprocess import Popen, PIPE

        p = Popen("pbcopy", stdin=PIPE)
        p.communicate(text.encode("utf-8"))
        return f"[ok] 已写入剪贴板（{len(text)} 字符）"
    else:
        return "[error] 非 Windows/macOS 请安装 pyperclip：pip install pyperclip"
