"""
沙箱与安全策略：所有工具都必须经过这一层。

为什么把沙箱独立成一个模块？
    因为「工具」会不断被扩展，而「边界」必须只有一处定义。
    插件作者只要 `from core import safe_path`，就自动继承了工作区限制；
    核心代码则完全不需要知道任何一个具体插件长什么样。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from ._paths import DEFAULT_WORKSPACE
from .cancel import current_token, kill_tree

# ---------------------------------------------------------------- 可调参数

# 没设 AGENT_WORKSPACE 时的兜底。为什么不是 os.getcwd()？
#   因为 cwd 取决于「用户在哪敲的命令」，而从任意目录跑 `python -m core`
#   都是合法用法。锚到 DEFAULT_WORKSPACE（用户数据目录），行为才可预测。
#   （测试通过 AGENT_WORKSPACE 把它指回项目根，好让 demo_case/ 里的用例生效。）
_WORKSPACE = os.path.abspath(os.environ.get("AGENT_WORKSPACE") or DEFAULT_WORKSPACE)
MAX_OUTPUT_CHARS = int(os.environ.get("AGENT_MAX_OUTPUT", "8000"))
ALLOW_DANGEROUS = os.environ.get("AGENT_ALLOW_DANGEROUS") == "1"

# 兼容老写法：`tools.WORKSPACE`（导入时快照）
WORKSPACE = _WORKSPACE


def workspace() -> str:
    """当前工作区根目录 —— 唯一可信来源。"""
    return _WORKSPACE


def set_workspace(path: str) -> str:
    """运行时切换工作区。请在加载插件之前调用。"""
    global _WORKSPACE, WORKSPACE
    _WORKSPACE = os.path.abspath(path)
    WORKSPACE = _WORKSPACE
    return _WORKSPACE


def set_max_output_chars(n: int) -> int:
    """运行时调整工具输出上限（Web UI 的「通用配置」改完立刻生效）。"""
    global MAX_OUTPUT_CHARS
    MAX_OUTPUT_CHARS = max(1, int(n))
    return MAX_OUTPUT_CHARS


def max_output_chars() -> int:
    """当前工具输出上限 —— 读模块变量而不是导入时快照，保证改完立刻生效。"""
    return MAX_OUTPUT_CHARS


# ---------------------------------------------------------------- 路径沙箱

class SandboxError(PermissionError):
    """路径越界。"""


def safe_path(p: str) -> str:
    """把相对路径解析到工作区内，并阻止越界访问。

    这是插件最常用的一个函数：任何要碰文件系统的工具都应该先调用它。
    """
    raw = p if os.path.isabs(p) else os.path.join(_WORKSPACE, p)
    ap = os.path.abspath(raw)
    root = os.path.normcase(_WORKSPACE)
    target = os.path.normcase(ap)
    if target != root and not target.startswith(root + os.sep):
        raise SandboxError(f"路径越界：{ap} 不在工作区 {_WORKSPACE} 内")
    return ap


# ---------------------------------------------------------------- 输出截断

def clip(text: str | None, limit: int | None = None) -> str:
    """截断过长输出 —— 上下文管理的第一道闸门。

    真实 Agent 里，一次 `cat 大文件` 就足以撑爆上下文窗口，
    而这个开销会在后续每一轮被重复计费。
    """
    if text is None:
        return ""
    limit = limit or MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    omitted = len(text) - limit
    return f"{head}\n\n… [已截断 {omitted} 字符，共 {len(text)} 字符] …\n\n{tail}"


# ---------------------------------------------------------------- 字节处理

def read_bytes(path: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    with open(path, "rb") as f:
        return f.read(max_bytes)


def looks_binary(data: bytes) -> bool:
    """含 NUL 字节基本就判定为二进制。"""
    return b"\x00" in data[:8192]


def decode_bytes(data: bytes) -> tuple[str, str]:
    """按 UTF-8 → GBK → Latin-1 顺序尝试解码，返回 (文本, 实际编码)。"""
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), "latin-1"


def hexdump(data: bytes, limit: int = 512) -> str:
    """生成十六进制预览（二进制降级时给模型看的）。"""
    head = data[:limit]
    return "\n".join(
        f"{i:08x}  {' '.join(f'{b:02x}' for b in head[i:i + 16])}"
        for i in range(0, len(head), 16)
    )


# ---------------------------------------------------------------- 命令黑名单

_BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+[/~]",
    r"\bformat\b",
    r"del\s+/[sfq]\b",
    r"shutdown\b",
    r"mkfs",
    r":\(\)\{",
    r"diskpart",
    r"reg\s+delete",
    r"rd\s+/s",
]


def check_command(command: str) -> str | None:
    """危险命令检查。返回 None 表示放行，否则返回拦截原因。

    注意：这是「兜底」，不是「主要防线」。
    主要防线是控制台层面的交互式确认（见 repl.py 的 /approve）。
    """
    if ALLOW_DANGEROUS:
        return None
    for pat in _BLOCKED_PATTERNS:
        if re.search(pat, command, re.I):
            return (
                f"该命令命中危险模式 `{pat}`，已被拦截。\n"
                f"确认要执行请设置环境变量 AGENT_ALLOW_DANGEROUS=1 后重试。"
            )
    return None


# ---------------------------------------------------------------- 可取消子进程

def run_process(
    cmd,
    *,
    shell: bool = False,
    cwd: str | None = None,
    timeout: float = 120,
    poll_interval: float = 0.15,
) -> tuple[int, str, str, bool]:
    """运行一个可被取消的子进程，返回 (returncode, stdout, stderr, was_cancelled)。

    为什么不用 subprocess.run？
      run() 会把主线程/工作线程**死死阻塞**在内部 wait 上：
        1. Ctrl+C 打不断（尤其 Windows），取消令牌也插不进去；
        2. 就算打断了，子进程还活着，在后台继续占资源。
    这里改用「Popen + 短轮询」：每 poll_interval 秒醒一次，
      · 发现令牌取消 → 杀掉整棵进程树，was_cancelled=True 返回；
      · 发现超时   → 杀掉整棵树，抛内建 TimeoutError。
    短轮询还有个副作用：每次 communicate(timeout=…) 醒来时 Python 都有机会
    处理信号，主线程场景下 Ctrl+C 也能及时送达。
    """
    token = current_token()
    if token is not None:
        token.check()   # 还没开始就被取消 —— 不必启动

    p = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # POSIX：让子进程自成进程组，killpg 才能一次杀干净
        start_new_session=(sys.platform != "win32"),
    )
    if token is not None:
        token.register_process(p)

    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = p.communicate(timeout=poll_interval)
                return int(p.returncode or 0), out or "", err or "", False
            except subprocess.TimeoutExpired:
                pass   # 没死透就继续轮询；已读到的输出不会丢
            if token is not None and token.cancelled():
                kill_tree(p)
                out, err = p.communicate()
                code = p.returncode if p.returncode is not None else -1
                return int(code), out or "", err or "", True
            if time.monotonic() >= deadline:
                kill_tree(p)
                p.communicate()
                # 对外抛内建 TimeoutError —— 调用方不用 import subprocess 也能接住
                raise TimeoutError(f"子进程超时（{timeout}s）")
    except BaseException:
        # 关键兜底：Ctrl+C 打断主线程轮询时，KeyboardInterrupt 会直接穿透到这里，
        # 而 finally 只负责 unregister —— 不补一刀的话子进程就成了孤儿。
        # BaseException 同时覆盖 KeyboardInterrupt / SystemExit / 协作式 Cancelled。
        kill_tree(p)
        try:
            p.communicate(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        if token is not None:
            token.unregister_process(p)
