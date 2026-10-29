"""
取消机制：让 Ctrl+C 真正能掐掉正在运行的任务。

为什么需要单独一个模块？
    没有它，Ctrl+C 在任务执行中只有两种下场：
      · 碰巧打断主线程正在等的一个点（如网络读）→ 子进程和工具线程继续在后台跑；
      · 打不断（线程池等待、子进程 wait）→ 表现为「按了没反应，过一会儿自己恢复」。
    真正的取消 = 一个**全局可见的开关**（令牌）+ 所有阻塞点**轮询它** + 令牌触发时
    **主动杀掉注册在案的子进程** + **把取消告知对端**。四件事缺一不可。

    第四件事（通知）容易被漏掉，因为漏了本地也「看起来正常」：
      · 只在自己这边停止等待，MCP server 并不知道，它会把那个慢工具跑到底；
      · 对外部世界来说，取消从未发生过。
    reason 字段就是为了把这句「我为什么不等了」完整地转述出去
    （见 core/mcp.py 的 notify_cancelled / notifications/cancelled）。

两级异常：
    Cancelled             对外：Agent.run() 被取消时抛出，REPL/入口捕获后友好提示。
    （内部协作信号也用 Cancelled —— 工具与 MCP 的阻塞点抛它，循环层统一归一化。）

谁需要关心这个模块？
    · 工具作者：`from core.cancel import current_token`，
      在长循环里 `tok.check()`（快速失败）或轮询 `tok.cancelled()`（优雅收尾）。
    · 需要强杀的子进程：拿到 Popen 后 `token.register_process(p)`，
      结束后 unregister —— cancel() 时它们会被整棵进程树杀掉。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Callable


class Cancelled(Exception):
    """任务被用户取消。Agent.run() 对外抛出的就是它。"""


def _taskkill(pid: int) -> None:
    """Windows 下按 pid 杀整棵树（子进程也一起走）。失败不外抛。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass


def kill_tree(p: subprocess.Popen, *, background: bool = False) -> None:
    """杀掉进程及其所有子进程。

    Windows 上 p.kill() 只杀直接子进程，shell=True 时真正的命令是 cmd.exe
    的孙进程 —— 所以必须用 taskkill /T 杀整棵树。
    POSIX 上配合 start_new_session=True，用进程组信号一次杀干净。

    background=True 时 Windows 的 taskkill 丢到后台线程：实测某些机器上
    单次 taskkill 就要 3s（Defender 实时扫描），同步调用会让「点停止」卡住。
    调用方若随后 p.wait() / communicate()，仍会等到进程真正退出。
    """
    if p.poll() is not None:
        return
    if sys.platform == "win32":
        if background:
            def _bg() -> None:
                _taskkill(p.pid)
                # taskkill 失败（进程已被别人杀掉/权限不足）时补一刀
                if p.poll() is None:
                    try:
                        p.kill()
                    except Exception:  # noqa: BLE001
                        pass

            threading.Thread(target=_bg, daemon=True, name="kill-tree").start()
            return
        _taskkill(p.pid)
        if p.poll() is None:      # taskkill 失败时兜底
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        return
    try:
        pgid = os.getpgid(p.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    # 只有当子进程自成一个进程组（start_new_session=True）时才能按组杀。
    # 否则它的组就是 agent 自己所在的组，killpg 会把 agent 连同一整棵会话树
    # 一起 SIGKILL —— 表现为「点停止，整个程序没了」。
    if pgid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except Exception:  # noqa: BLE001 —— 组杀失败时不外抛，退回只杀 pid
            pass
    try:
        p.kill()
    except Exception:  # noqa: BLE001 —— 杀进程失败不该让取消流程卡死
        pass


# 兼容旧私有名（sandbox.py 历史上这么引用）；新代码请直接用 kill_tree
_kill_tree = kill_tree


class CancellationToken:
    """一次任务运行的取消开关。线程安全。

    典型用法（Agent 循环）：
        token = CancellationToken()
        token.on_cancel(lambda: client.close())   # 掐断卡住的 HTTP 读
        ...
        if token.cancelled(): raise Cancelled     # 或 token.check()

    典型用法（工具内部）：
        token = current_token()
        if token:
            token.register_process(p)             # 让取消能杀掉我的子进程
            try:
                while working:
                    if token.cancelled(): ...     # 优雅收尾
            finally:
                token.unregister_process(p)
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._procs: set[subprocess.Popen] = set()
        self._hooks: list[Callable[[], None]] = []
        self._reason = ""

    # ---------------------------------------------------------- 查询

    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """谁（以什么理由）触发了取消。

        它不是装饰性字段：MCP 的取消通知要把 reason 转述给对端
        （`notifications/cancelled {requestId, reason}`），UI 也要靠它
        向人解释「为什么这一轮停了」。没填过就退回一句默认值。
        """
        return self._reason or "任务被取消（未说明原因）"

    def check(self) -> None:
        """已取消则抛 Cancelled —— 给「快速失败」的检查点用。"""
        if self._event.is_set():
            raise Cancelled(self.reason)

    # ---------------------------------------------------------- 触发

    def cancel(self, reason: str = "") -> None:
        """触发取消：置位 + 杀掉全部注册的子进程 + 逐个执行钩子。

        幂等：重复调用没有副作用（第二次直接返回）。
        顺序刻意安排为「先发起杀进程、再跑钩子」—— 钩子里常用 client.close()
        来掐断网络读，先杀进程能保证本地副作用先清干净。
        Windows 上 tree-kill 走后台线程，本方法不阻塞。
        reason 只在第一次生效，用来解释「为什么取消」。重复调用时不影响已有原因。
        """
        with self._lock:
            if self._event.is_set():
                return
            if reason:
                self._reason = reason
            self._event.set()
            procs = list(self._procs)
            hooks = list(self._hooks)
        for p in procs:
            # 后台杀：taskkill 在部分 Windows 机器上要 3s，不能让它拖住停止按钮
            kill_tree(p, background=True)
        for h in hooks:
            try:
                h()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- 注册

    def on_cancel(self, hook: Callable[[], None]) -> None:
        """注册取消时的回调（如关闭网络客户端）。已在取消状态则立即执行。"""
        with self._lock:
            if not self._event.is_set():
                self._hooks.append(hook)
                return
        try:
            hook()
        except Exception:  # noqa: BLE001
            pass

    def register_process(self, p: subprocess.Popen) -> None:
        """登记一个子进程；cancel() 时会被整棵树杀掉。"""
        with self._lock:
            if self._event.is_set():
                # 注册前已经取消 —— 立刻杀，别让它活到 unregister
                kill_tree(p, background=True)
                return
            self._procs.add(p)

    def unregister_process(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(p)


# ---------------------------------------------------------------- 当前令牌

# 「当前正在运行的任务」的令牌。
# 一次只有一个 Agent.run() 在跑（REPL 是串行对话模型），模块级变量足够；
# 用线程局部反而会让「工具运行在工作线程」这件事读不到令牌。
_current: CancellationToken | None = None
_current_lock = threading.Lock()


def current_token() -> CancellationToken | None:
    """工具内部用它拿到当前任务的取消令牌（没有则为 None —— 不取消也不报错）。"""
    return _current


def set_current_token(token: CancellationToken | None) -> CancellationToken | None:
    """设置当前令牌，返回旧值（供恢复）。Agent.run() 的进入/退出配对使用。"""
    global _current
    with _current_lock:
        previous = _current
        _current = token
    return previous
