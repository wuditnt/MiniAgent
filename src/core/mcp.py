"""
MCP（Model Context Protocol）客户端 —— 扩展通道之二。

与「工具目录」的区别：
    工具目录：你自己写 Python 函数，进程内执行。
    MCP     ：挂载别人写好的工具服务，独立进程，通过 stdio 上的 JSON-RPC 通信。

对 Agent 循环来说两者没有区别 —— MCP 工具会被桥接成同样的 `Tool` 对象，
进入同一个 REGISTRY，一起出现在发给模型的 tools 数组里。

为什么手写而不用官方 SDK？
    官方 SDK 是 async 的，而本项目的循环是同步的；为了一个握手协议引入
    async 桥接层会让核心循环复杂化。这里只实现 tools/list 与 tools/call
    两个必需方法，约 200 行、零依赖、完全可控。

线程安全：
    同一个 server 的连接可以被多个线程并发调用（agent 会并行执行工具）。
    实现方式是「每个在途请求一个专属队列，响应按 id 精确投递」——
    绝不能写成「从公共队列取，id 不匹配就丢掉」，那样并发时线程之间会互相
    取走对方的响应，表现为随机超时，而且只在并发时才复现。

取消通知（notifications/cancelled）：
    「取消」有两半 —— 自己不再等，以及**告诉对端别再干了**。
    只做前一半时，本端看起来一切正常（工具立刻返回 [cancelled]），
    但服务端那个慢工具还在傻跑：白占 CPU、白占连接、白占配额。
    本模块按 MCP 2024-11-05 规范补上后一半：
        · 在途请求被取消 / 超时 → 发 notifications/cancelled {requestId, reason}
        · 通知幂等（同一 id 只发一次）
        · 对端仍把响应发来了（它没来得及停）→ 静默丢弃，绝不污染后续调用
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from core import pyexe
from core.cancel import Cancelled, current_token
from core.logging_setup import get_logger
from core.registry import Tool, ToolRegistry

_log = get_logger("app")

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "mini-agent"
CLIENT_VERSION = "0.3"

MCP_POLL_INTERVAL = 0.15   # 等待响应的轮询步长（秒）：取消令牌的响应延迟上限

# 已发出的取消通知保留多少个 requestId。
# 只用于「这条迟到的响应属于哪个已放弃的请求」的判断，没必要永久保存；
# 256 个足够覆盖任何正常会话的并发深度。
_CANCELLED_ID_MEMORY = 256

_NAME_OK = re.compile(r"[^A-Za-z0-9_-]")


class MCPError(RuntimeError):
    """MCP 协议层错误。"""


#: `python` / `python3` / `python3.12` / `python.exe` 这类「裸解释器名」
_PY_EXE_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?(?:\.exe)?$", re.IGNORECASE)
#: Windows 的 py 启动器（`py` / `py.exe`）
_PY_LAUNCHER_RE = re.compile(r"^py(?:\.exe)?$", re.IGNORECASE)


def _bare_name(command: str) -> str:
    """取出「裸命令名」；带目录的命令返回空串（视为用户显式指定，不改写）。"""
    name = command.strip().strip('"')
    if os.path.dirname(name):
        return ""
    return name


def _is_python_command(command: str) -> bool:
    """命令是否为裸解释器名（python / python3 / python3.12 / py）。"""
    name = _bare_name(command)
    return bool(name and (_PY_EXE_RE.match(name) or _PY_LAUNCHER_RE.match(name)))


def _resolve_command(command: str) -> str:
    """把常见的 `python` 写法映射到当前解释器，避免多环境错乱。

    「当前解释器」由 MINI_AGENT_PYTHON 决定（见 core/pyexe.py），与 run_python
    使用同一个 Python —— 否则 MCP 服务端可能跑在另一个环境里。
    含目录的绝对路径视为用户明确指定，原样保留。
    """
    if _is_python_command(command):
        return pyexe.executable()
    return command


def _resolve_argv(command: str, args: list[str]) -> list[str]:
    """改写 MCP 服务端的启动命令。

    除了换解释器，还要摘掉 py 启动器的版本参数：`py -3` / `py -3.12`
    里的 `-3` 是 py.exe 的选项，直接传给 python.exe 会报错。
    """
    if not _PY_LAUNCHER_RE.match(_bare_name(command)):
        return [_resolve_command(command), *args]
    rest = list(args)
    while rest and re.match(r"^-[23](?:\.\d+)?$", rest[0]):
        rest.pop(0)
    return [pyexe.executable(), *rest]


def fq_tool_name(server: str, tool: str) -> str:
    """拼出合法且唯一的模型侧函数名：server__tool。

    OpenAI function calling 的 name 只允许 [A-Za-z0-9_-] 且不超过 64 字符，
    所以这里必须清洗 + 截断，并且保证截断后仍然唯一。
    """
    raw = f"{_NAME_OK.sub('_', server)}__{_NAME_OK.sub('_', tool)}"
    if len(raw) <= 64:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return raw[: 64 - 9] + "_" + digest


@dataclass
class MCPServerState:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    status: str = "pending"      # pending / ready / failed / stopped
    error: str = ""
    tools: list[str] = field(default_factory=list)        # 已注册的模型侧工具名
    all_tools: list[dict] = field(default_factory=list)   # 服务端发现的全部工具（含被禁用项）
    server_info: dict = field(default_factory=dict)
    transport: str = "stdio"
    url: str = ""
    # 失败可见性：启动耗时、尝试次数、stderr 尾部
    started_ms: int = 0
    attempts: int = 1
    stderr_tail: list[str] = field(default_factory=list)


class MCPStdioClient:
    """一个 MCP server 的同步 stdio 客户端。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.cwd = cwd
        self.env = dict(env or {})
        self.timeout = timeout

        self._proc: subprocess.Popen | None = None
        self._rx: queue.Queue[dict] = queue.Queue()
        # 每个在途请求一个专属队列。为什么不用「从 _rx 里取，id 不匹配就丢掉」？
        # 因为那在并发下是错的：A 线程会把 B 线程的响应取走并丢弃，B 只能等到超时。
        # 并行工具执行（agent._run_tools）会真的并发调用同一个 server，这个坑必须堵上。
        self._waiters: dict[int, queue.Queue[dict]] = {}
        self._inflight: dict[int, str] = {}          # rid -> method（诊断与统计用）
        # 已发过取消通知的 requestId。迟到的响应靠它认领并丢弃。
        # deque + 定长：老 id 自然淘汰，不会无限增长；append/`in` 在 CPython 下是原子的。
        self._cancelled_ids: collections.deque[int] = collections.deque(
            maxlen=_CANCELLED_ID_MEMORY
        )
        self._late_log: collections.deque[str] = collections.deque(maxlen=64)
        self._stderr: collections.deque[str] = collections.deque(maxlen=200)
        self._stdout_noise: collections.deque[str] = collections.deque(maxlen=200)
        self._next_id = 1
        self._send_lock = threading.Lock()
        self.server_info: dict = {}
        # 服务端主动推送的通知（tools/list_changed 等）交给 manager 处理
        self.on_notification = None
        # 取消相关的可观测数字：界面与测试都靠它断言「通知真的发了」
        self.stats: dict[str, int] = {
            "requests": 0, "cancellations": 0, "late_responses": 0, "timeouts": 0,
        }

    # ---------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._proc is not None:
            return
        # PATH 前置解释器目录 + UTF-8 stdio（pyexe.subprocess_env 统一加工）：
        # 服务端脚本内部再调 `python` / `pip` 也会命中同一个环境。
        full_env = pyexe.subprocess_env({**os.environ, **self.env})
        # MCP 的 stdio 协议是 UTF-8 JSON：必须显式把子进程的编码定成 UTF-8。
        # 否则在非中文代码页（英文 Windows 的 cp1252）上，服务端输出中文时
        # 会在 stdout.write 处抛 UnicodeEncodeError，表现为「tools/list 超时」。
        # 用 setdefault：用户在 mcp_servers.json 里显式配置的 env 优先。
        full_env.setdefault("PYTHONUTF8", "1")
        try:
            self._proc = subprocess.Popen(
                _resolve_argv(self.command, self.args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=full_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise MCPError(f"启动失败，找不到命令：{self.command}") from e

        threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True).start()

        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self.notify("notifications/initialized", {})

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 5.0) -> None:
        if self._proc is None:
            return
        # 先唤醒所有在途等待者：连接马上就没了，它们不该再各自等到超时。
        # 不做这一步的表现是「关掉 MCP server 之后程序卡住几十秒」——
        # _read_stdout 随进程退出而结束，再也没有人往 waiter 里投递。
        self._fail_pending(f"MCP server「{self.name}」连接已关闭")
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        finally:
            self._proc = None

    def _fail_pending(self, reason: str) -> None:
        """把所有在途请求用一条错误帧唤醒。"""
        with self._send_lock:
            waiters = list(self._waiters.values())
        for q in waiters:
            try:
                q.put_nowait(
                    {"jsonrpc": "2.0", "id": -1,
                     "error": {"code": -32000, "message": reason}}
                )
            except queue.Full:   # 队列满说明响应已经先到了，无需干预
                pass

    # ---------------------------------------------------------- 后台读线程

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 有些 server 会把日志写到 stdout —— 收起来供排查，不当协议消息
                self._stdout_noise.append(line)
                continue
            self._dispatch(msg)
        # 走到这里 = stdout 关闭 = server 进程结束（正常关机或意外崩溃）。
        # 唤醒在途请求，别让它们干等到自己的超时。
        code = self._proc.returncode if self._proc is not None else None
        self._fail_pending(f"MCP server「{self.name}」进程已退出（code={code}）")

    def _dispatch(self, msg: dict) -> None:
        """按 id 把响应投递给等它的那个线程；其余（通知等）进公共队列。"""
        # 合法的 JSON 行不一定是对象：server 可能往 stdout 打一个裸字面量
        # （[] / 123 / "ok" / null）。直接 msg.get(...) 会抛 AttributeError，
        # 把读线程打死 —— 之后所有请求都只能等超时。这类行按噪声收起来。
        if not isinstance(msg, dict):
            self._stdout_noise.append(json.dumps(msg, ensure_ascii=False))
            return
        rid = msg.get("id")
        if isinstance(rid, int):
            waiter = self._waiters.get(rid)
            if waiter is not None:
                waiter.put(msg)
                return
            if rid in self._cancelled_ids:
                # 我们已经发过取消通知、也不再等它了，但响应还是来了
                # （对端没处理完 / 通知在路上）。静默丢弃。
                # 绝不能塞回 _rx：那等于把一条迟到的工具结果伪装成
                # 「server 主动发来的消息」，会污染后续调用者的视野。
                self.stats["late_responses"] += 1
                self._late_log.append(
                    f"id={rid} 的响应在对端收到取消通知前就已发出，已被丢弃"
                )
                return
        self._rx.put(msg)
        # 无 id + 有 method = 服务端通知。读线程不能在这里做重活，
        # 回调由 manager 负责调度（它只登记定时器/写日志）。
        if "method" in msg and msg.get("id") is None and self.on_notification is not None:
            try:
                self.on_notification(msg)
            except Exception:  # noqa: BLE001 —— 通知处理出错不能让读线程退出
                _log.exception("mcp.notification_failed name=%s", self.name)

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())

    # ---------------------------------------------------------- 协议收发

    def _send(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("连接尚未建立")
        with self._send_lock:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def notify_cancelled(self, rid: int, reason: str = "") -> bool:
        """告诉对端「这个请求我不要了」—— 取消通知的另一半。

        规范（MCP 2024-11-05）：
            method : notifications/cancelled
            params : {requestId, reason?}
            接收方 SHOULD 停止处理该请求，且 SHOULD NOT 再回响应；
            通知本身不需要响应 —— 它是一次单向表态，不是请求。

        为什么必须发：只在自己的等待循环里放弃是**单方面的**。对端不知道，
        会把那个慢请求当正常请求跑到底 —— 一个 `sleep(60)` 的服务端工具
        会白占 60 秒。发了通知，对端才有机会提前收手。

        幂等：同一 requestId 只发一次（重复通知只会打扰对端）。
        永不抛异常：连接已断时通知本就没有意义，不该把取消流程带崩。
        返回 True 表示这次真的发出去了。
        """
        with self._send_lock:
            if rid in self._cancelled_ids:
                return False
            # 先登记、后发送：响应可能恰好在发通知的瞬间到达，
            # _dispatch 必须先能看到「这个 id 已被放弃」，才敢丢弃它。
            self._cancelled_ids.append(rid)
        try:
            self.notify("notifications/cancelled", {"requestId": rid, "reason": reason})
        except Exception:  # noqa: BLE001 —— 管道断了就算了
            return False
        self.stats["cancellations"] += 1
        return True

    @staticmethod
    def _cancel_reason(method: str, token) -> str:
        """把本地的取消原因转述成对端能看懂的一句话。"""
        why = getattr(token, "reason", "") or "用户取消了任务"
        return f"{why}；客户端已中止等待 {method} 的响应"

    def request(self, method: str, params: dict | None = None) -> dict:
        # 先登记等待队列，再发请求 —— 顺序反过来的话，响应可能在登记前就到了，
        # 那一帧会被当成「无人认领」丢进 _rx，本调用就只能等超时。
        inbox: queue.Queue[dict] = queue.Queue(maxsize=8)
        with self._send_lock:
            rid = self._next_id
            self._next_id += 1
            self._waiters[rid] = inbox
            self._inflight[rid] = method
            self.stats["requests"] += 1

        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

            deadline = time.time() + self.timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.stats["timeouts"] += 1
                    # 超时同样属于「我不再等这个响应了」：按规范补一张取消通知，
                    # 否则服务端会为一个没人要的结果跑到天荒地老。
                    self.notify_cancelled(
                        rid, f"客户端等待 {method} 超时（{self.timeout}s），已放弃该请求的响应"
                    )
                    raise MCPError(f"{method} 超时（{self.timeout}s）{self._diagnostics()}")
                # 短轮询而不是一次 get(timeout=remaining)：
                # 取消令牌触发时能在 poll_interval 内醒来；迟到的响应会掉进
                # 已被移除的 waiter（finally 里弹出），不会污染下一次调用。
                try:
                    msg = inbox.get(timeout=min(remaining, MCP_POLL_INTERVAL))
                except queue.Empty:
                    tok = current_token()
                    if tok is not None:
                        try:
                            tok.check()
                        except Cancelled:
                            # 本端到这里就可以走了，但对端还在干活 —— 通知它。
                            self.notify_cancelled(rid, self._cancel_reason(method, tok))
                            raise
                    continue

                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(f"{method} 返回错误：{err.get('code')} {err.get('message')}")
                result = msg.get("result", {})
                return result if isinstance(result, dict) else {"_raw": result}
        finally:
            with self._send_lock:
                self._waiters.pop(rid, None)
                self._inflight.pop(rid, None)

    def inflight(self) -> dict[int, str]:
        """当前在途请求（rid -> method）的快照，供诊断使用。"""
        with self._send_lock:
            return dict(self._inflight)

    def late_responses(self) -> list[str]:
        """被丢弃的迟到响应记录 —— 证明「对端确实没来得及收手」。"""
        return list(self._late_log)

    def _diagnostics(self) -> str:
        bits = []
        if self._proc is not None and self._proc.poll() is not None:
            bits.append(f"进程已退出（code={self._proc.returncode}）")
        if self._stderr:
            bits.append("stderr: " + " / ".join(list(self._stderr)[-3:])[:400])
        return ("\n  " + "\n  ".join(bits)) if bits else ""

    # ---------------------------------------------------------- 高层方法

    def list_tools(self) -> list[dict]:
        result = self.request("tools/list", {})
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return self._render_result(result)

    @staticmethod
    def _render_result(result: dict) -> str:
        """把 MCP 的 content 数组渲染成一段文本给模型看。"""
        chunks: list[str] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                chunks.append(str(item))
                continue
            kind = item.get("type")
            if kind == "text":
                chunks.append(item.get("text", ""))
            elif kind == "resource":
                res = item.get("resource", {})
                chunks.append(f"[resource {res.get('uri', '?')}]")
            elif kind == "image":
                chunks.append(f"[image {item.get('mimeType', '?')}]")
            else:
                chunks.append(json.dumps(item, ensure_ascii=False)[:500])

        if not chunks and "structuredContent" in result:
            chunks.append(json.dumps(result["structuredContent"], ensure_ascii=False))

        text = "\n".join(c for c in chunks if c).strip() or "(空结果)"
        if result.get("isError"):
            return f"[mcp error] {text}"
        return text

    def stderr_tail(self, n: int = 5) -> list[str]:
        return list(self._stderr)[-n:]


# ---------------------------------------------------------------- HTTP 传输

class MCPHttpClient:
    """Streamable HTTP 传输的 MCP 客户端（零依赖：urllib）。

    协议要点：
      · 每个 JSON-RPC 消息一次 POST；响应可能是 application/json，
        也可能是 text/event-stream（SSE 帧里挑出对应 id 的那条）；
      · initialize 响应头里的 Mcp-Session-Id 必须带到后续所有请求；
      · 服务端通知（tools/list_changed / message）通过 GET 同一个地址
        的长连接 SSE 推送，不支持时安静降级（不会反复重连）。
    """

    def __init__(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.url = url
        self.headers = {str(k): str(v) for k, v in (headers or {}).items() if k}
        self.timeout = timeout
        self.server_info: dict = {}
        self.on_notification = None
        self.stats: dict[str, int] = {
            "requests": 0, "cancellations": 0, "late_responses": 0, "timeouts": 0,
        }
        self._session_id = ""
        self._next_id = 1
        self._closed = False
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._errors: collections.deque[str] = collections.deque(maxlen=50)
        self._cancelled_ids: collections.deque[int] = collections.deque(
            maxlen=_CANCELLED_ID_MEMORY
        )

    # ---------------------------------------------------------- 生命周期

    def start(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self.notify("notifications/initialized", {})
        # 通知流单独一个线程：拿不到（405/404）就静默放弃，不影响工具调用
        threading.Thread(
            target=self._read_notifications, name=f"mcp-{self.name}-sse", daemon=True
        ).start()

    def alive(self) -> bool:
        return not self._closed

    def stop(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        # 规范允许客户端 DELETE 结束会话；失败无所谓（服务端可能不支持）
        try:
            req = urllib.request.Request(
                self.url, method="DELETE", headers=self._http_headers()
            )
            urllib.request.urlopen(req, timeout=min(timeout, 3.0)).close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- 协议收发

    def _http_headers(self, accept: str = "application/json, text/event-stream") -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        headers.update(self.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict) -> list[dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, method="POST", headers=self._http_headers()
        )
        method = payload.get("method", "?")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    return self._read_sse(resp)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            self._errors.append(f"HTTP {e.code} {e.reason} {detail}".strip())
            raise MCPError(
                f"{method} HTTP {e.code} {e.reason}" + (f"：{detail}" if detail else "")
            ) from e
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "timed out" in reason or isinstance(e.reason, TimeoutError):
                self.stats["timeouts"] += 1
                raise MCPError(f"{method} 超时（{self.timeout}s）") from e
            self._errors.append(f"连接失败：{reason}")
            raise MCPError(f"无法连接 {self.url}：{reason}") from e
        except TimeoutError as e:
            self.stats["timeouts"] += 1
            raise MCPError(f"{method} 超时（{self.timeout}s）") from e
        except OSError as e:
            self._errors.append(f"网络错误：{e}")
            raise MCPError(f"{method} 网络错误：{e}") from e

        if not raw.strip():
            return []
        try:
            return [json.loads(raw)]
        except json.JSONDecodeError as e:
            raise MCPError(f"{method} 返回了非法 JSON") from e

    @staticmethod
    def _read_sse(stream) -> list[dict]:
        """读一个 SSE 流，返回其中所有 JSON 消息（响应 + 通知）。"""
        events: list[dict] = []
        buffer: list[str] = []
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if buffer:
                    try:
                        events.append(json.loads("\n".join(buffer)))
                    except json.JSONDecodeError:
                        pass
                    buffer = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())
        if buffer:
            try:
                events.append(json.loads("\n".join(buffer)))
            except json.JSONDecodeError:
                pass
        return [e for e in events if isinstance(e, dict)]

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def notify_cancelled(self, rid: int, reason: str = "") -> bool:
        with self._send_lock:
            if rid in self._cancelled_ids:
                return False
            self._cancelled_ids.append(rid)
        try:
            self.notify("notifications/cancelled", {"requestId": rid, "reason": reason})
        except Exception:  # noqa: BLE001
            return False
        self.stats["cancellations"] += 1
        return True

    def request(self, method: str, params: dict | None = None) -> dict:
        tok = current_token()
        if tok is not None:
            tok.check()
        with self._send_lock:
            rid = self._next_id
            self._next_id += 1
        self.stats["requests"] += 1
        try:
            messages = self._post(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            )
        except MCPError:
            raise
        except Cancelled:
            self.notify_cancelled(rid, "客户端取消了该请求")
            raise

        response: dict | None = None
        for msg in messages:
            if msg.get("id") == rid:
                response = msg
            elif msg.get("method"):
                self._emit(msg)
        if response is None:
            raise MCPError(f"{method} 未返回响应（收到 {len(messages)} 条消息）")
        if "error" in response:
            err = response["error"]
            raise MCPError(
                f"{method} 返回错误：{err.get('code')} {err.get('message')}"
            )
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"_raw": result}

    def _emit(self, msg: dict) -> None:
        if self.on_notification is None:
            return
        try:
            self.on_notification(msg)
        except Exception:  # noqa: BLE001
            _log.exception("mcp.notification_failed name=%s", self.name)

    # ---------------------------------------------------------- 通知长连接

    def _read_notifications(self) -> None:
        while not self._stop.is_set():
            req = urllib.request.Request(
                self.url,
                method="GET",
                headers=self._http_headers(accept="text/event-stream"),
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=max(self.timeout, 30.0)
                ) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "text/event-stream" not in ctype:
                        return  # 服务端不支持通知流：安静退出
                    buffer: list[str] = []
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if buffer:
                                try:
                                    msg = json.loads("\n".join(buffer))
                                except json.JSONDecodeError:
                                    msg = None
                                if isinstance(msg, dict):
                                    self._emit(msg)
                                buffer = []
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            buffer.append(line[5:].lstrip())
            except urllib.error.HTTPError as e:
                if e.code in (404, 405, 501):
                    return  # 不支持独立通知流
                self._stop.wait(1.0)
            except Exception:  # noqa: BLE001 —— 空闲超时/断线都按重连处理
                if self._stop.is_set():
                    return
                self._stop.wait(1.0)

    # ---------------------------------------------------------- 诊断与高层方法

    def inflight(self) -> dict[int, str]:
        return {}

    def late_responses(self) -> list[str]:
        return []

    def stderr_tail(self, n: int = 5) -> list[str]:
        return list(self._errors)[-n:]

    def list_tools(self) -> list[dict]:
        result = self.request("tools/list", {})
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return MCPStdioClient._render_result(result)


# ---------------------------------------------------------------- 桥接成 Tool

def tools_from_client(
    client,
    registry: ToolRegistry,
    group: str | None = None,
    dangerous: bool = True,
    disabled: set[str] | None = None,
    discovered: list[dict] | None = None,
) -> list[str]:
    """把一个 MCP server 的工具注册进注册表，返回注册成功的工具名。

    disabled 是服务端原始工具名集合（配置里的 disabled_tools）；
    discovered 可复用已经 list_tools() 拿到的结果，避免重复请求服务端。
    """
    disabled = disabled or set()
    specs = discovered if discovered is not None else client.list_tools()
    registered: list[str] = []
    for spec in specs:
        raw_name = spec.get("name")
        if not raw_name or raw_name in disabled:
            continue
        fq = fq_tool_name(client.name, raw_name)
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}

        # 注意闭包陷阱：用默认参数把 raw_name / client 绑进当前迭代
        def _make(tool_name: str, cli: MCPStdioClient):
            def _call(**kwargs) -> str:
                return cli.call_tool(tool_name, kwargs)
            return _call

        t = Tool(
            name=fq,
            description=(spec.get("description") or f"来自 MCP server「{client.name}」的工具").strip(),
            parameters=schema,
            func=_make(raw_name, client),
            group=group or f"MCP:{client.name}",
            dangerous=dangerous,
            source="mcp",
            module=f"mcp:{client.name}",
            origin=client.name,
            tags=("mcp",),
        )
        try:
            registry.add(t)
            registered.append(fq)
        except ValueError:
            continue
    return registered


# ---------------------------------------------------------------- 配置加载

@dataclass
class MCPReport:
    servers: dict[str, MCPServerState] = field(default_factory=dict)

    def ready(self) -> list[str]:
        return [n for n, s in self.servers.items() if s.status == "ready"]

    def failed(self) -> list[str]:
        return [n for n, s in self.servers.items() if s.status == "failed"]

    def tool_count(self) -> int:
        return sum(len(s.tools) for s in self.servers.values())

    def summary(self) -> str:
        if not self.servers:
            return "未配置 MCP server"
        ready, failed = len(self.ready()), len(self.failed())
        return (
            f"server {len(self.servers)} 个（就绪 {ready} / 失败 {failed}）"
            f" | 工具 {self.tool_count()} 个"
        )


DEFAULT_CONFIG = {"servers": {}}


def load_config(path: str) -> dict:
    if not os.path.isfile(path):
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MCPError(f"读取 {path} 失败：{e}") from e
    if "servers" not in data or not isinstance(data["servers"], dict):
        raise MCPError(f"{path} 格式不对，应形如 {{\"servers\": {{...}}}}")
    return data


class MCPManager:
    """管理所有 MCP server 的连接与生命周期。"""

    def __init__(self, config_path: str, project_root: str, registry: ToolRegistry) -> None:
        self.config_path = config_path
        self.root = project_root
        self.registry = registry
        self.clients: dict[str, MCPStdioClient] = {}
        self.report = MCPReport()
        # 界面上保存/启停会触发重载；重载要串行，避免两个请求同时改 clients / 注册表
        self._load_lock = threading.RLock()
        # 服务端日志环与工具变更通知的防抖定时器
        self._logs: dict[str, collections.deque[str]] = {}
        self._reload_timers: dict[str, threading.Timer] = {}
        self._notify_lock = threading.Lock()

    def load(self, only: list[str] | None = None) -> MCPReport:
        with self._load_lock:
            return self._load(only)

    def _load(self, only: list[str] | None = None) -> MCPReport:
        try:
            cfg = load_config(self.config_path)
        except MCPError as e:
            self.report = MCPReport()
            self.report.servers["(配置)"] = MCPServerState(
                name="(配置)", command="", status="failed", error=str(e)
            )
            return self.report

        report = MCPReport()
        for name, spec in cfg["servers"].items():
            if only and name not in only:
                if name in self.report.servers:
                    report.servers[name] = self.report.servers[name]
                continue
            if not spec.get("enabled", True):
                report.servers[name] = MCPServerState(
                    name=name, command=spec.get("command", ""), status="stopped", error="配置中已禁用"
                )
                continue

            state = MCPServerState(
                name=name,
                command=spec.get("command", ""),
                args=list(spec.get("args", [])),
                transport=spec.get("transport", "stdio"),
                url=spec.get("url", ""),
            )

            cwd = spec.get("cwd")
            cwd = os.path.join(self.root, cwd) if cwd and not os.path.isabs(cwd) else cwd or self.root
            disabled = set(spec.get("disabled_tools") or [])
            started_at = time.time()
            attempts = 0
            client = None
            while True:
                # 失败后自动重试一次：多数失败是服务端首次启动慢或瞬时握手失败，
                # 给用户看到的错误里会注明重试过，连接成功时则无感。
                attempts += 1
                client = self._make_client(name, spec, cwd)
                # 服务端主动通知（tools/list_changed 等）统一交给 manager 调度
                client.on_notification = (
                    lambda msg, _name=name: self._handle_notification(_name, msg)
                )
                try:
                    client.start()
                    self.registry.remove_module(f"mcp:{name}")
                    specs = client.list_tools()
                    state.all_tools = [
                        {
                            "raw_name": item.get("name", ""),
                            "description": item.get("description", ""),
                            "dangerous": bool(spec.get("dangerous", True)),
                        }
                        for item in specs if item.get("name")
                    ]
                    state.tools = tools_from_client(
                        client,
                        self.registry,
                        group=spec.get("group"),
                        dangerous=bool(spec.get("dangerous", True)),
                        disabled=disabled,
                        discovered=specs,
                    )
                    state.server_info = client.server_info
                    state.status = "ready"
                    old = self.clients.get(name)
                    self.clients[name] = client
                    if old is not None and old is not client:
                        # 重载/重启同名 server：旧连接已没人持有，必须关掉 ——
                        # 否则子进程与读线程会一直挂着（每次 reload 泄漏一个）。
                        old.stop()
                    break
                except Exception as e:  # noqa: BLE001
                    err = str(e) if isinstance(e, MCPError) else f"{type(e).__name__}: {e}"
                    state.error = err
                    state.stderr_tail = client.stderr_tail()
                    client.stop()
                    if attempts < 2:
                        time.sleep(0.3)
                        continue
                    state.status = "failed"
                    state.error = f"{err}（已自动重试 {attempts - 1} 次）"
                    _log.error("mcp.start_failed name=%s error=%s stderr=%s",
                               name, err, " / ".join(state.stderr_tail)[:400])
                    break

            state.attempts = attempts
            state.started_ms = int((time.time() - started_at) * 1000)
            report.servers[name] = state

        # 关掉不再需要的连接，并摘掉它们的工具：
        # 服务被禁用（enabled:false）或启动失败时，旧工具若留在注册表里，
        # 模型仍会看到并调用一个已经没有后端的工具 —— 必须随连接一起下线。
        for name in list(self.clients):
            if name not in report.servers or report.servers[name].status != "ready":
                self.clients[name].stop()
                self.clients.pop(name, None)
                self.registry.remove_module(f"mcp:{name}")

        self.report = report
        return report

    def _make_client(self, name: str, spec: dict, cwd: str):
        """按传输方式创建客户端；两种传输对外接口完全一致。"""
        if spec.get("transport") == "http":
            return MCPHttpClient(
                name=name,
                url=spec.get("url", ""),
                headers=spec.get("headers") or {},
                timeout=float(spec.get("timeout", 30)),
            )
        return MCPStdioClient(
            name=name,
            command=spec.get("command", ""),
            args=list(spec.get("args") or []),
            cwd=cwd,
            env=spec.get("env") or {},
            timeout=float(spec.get("timeout", 30)),
        )

    # ------------------------------------------------------ 服务端通知

    def _append_log(self, name: str, text: str) -> None:
        with self._notify_lock:
            ring = self._logs.get(name)
            if ring is None:
                ring = collections.deque(maxlen=100)
                self._logs[name] = ring
            ring.append(f"{time.strftime('%H:%M:%S')} {text}")

    def _handle_notification(self, name: str, msg: dict) -> None:
        """客户端读线程调进来：这里只能做登记类工作，不能阻塞。"""
        method = str(msg.get("method") or "")
        if method == "notifications/tools/list_changed":
            self._schedule_reload(name)
        elif method == "notifications/message":
            params = msg.get("params") or {}
            level = str(params.get("level") or "info")
            data = params.get("data")
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            self._append_log(name, f"[{level}] {text}")
        # resources/prompts 的变更通知暂不消费

    def _schedule_reload(self, name: str) -> None:
        """tools/list_changed 防抖重载：同一连接短时间多次通知只重载一次。"""
        with self._notify_lock:
            old = self._reload_timers.pop(name, None)
            if old is not None:
                old.cancel()
            timer = threading.Timer(0.5, self._reload_after_notification, args=(name,))
            timer.daemon = True
            self._reload_timers[name] = timer
            timer.start()

    def _reload_after_notification(self, name: str) -> None:
        with self._notify_lock:
            self._reload_timers.pop(name, None)
        try:
            self.reload(name)
            _log.info("mcp.tools_changed_reloaded name=%s", name)
        except Exception:  # noqa: BLE001
            _log.exception("mcp.tools_changed_reload_failed name=%s", name)

    def shutdown(self) -> None:
        with self._notify_lock:
            for timer in self._reload_timers.values():
                timer.cancel()
            self._reload_timers.clear()
        for client in self.clients.values():
            client.stop()
        self.clients.clear()

    def reload(self, name: str) -> MCPReport:
        """只重载一个连接器（新增/修改/删除都走这里）。

        删除时配置里已经没有这个名字，`_load(only=[name])` 的收尾逻辑会关掉旧连接、
        摘掉它的工具，所以不需要单独的 delete 路径。
        """
        return self.load(only=[str(name)])

    def snapshot(self) -> dict:
        """给界面用的只读快照：连接状态 + 工具清单 + 取消统计。"""
        with self._load_lock:
            states = list(self.report.servers.values())
            cancel = self.cancel_stats()
        by_module: dict[str, list] = {}
        for t in self.registry.all(source="mcp"):
            by_module.setdefault(t.module, []).append(t)
        servers = []
        for st in states:
            registry_tools = by_module.get(f"mcp:{st.name}", [])
            registered_fq = {t.name for t in registry_tools}
            if st.all_tools:
                tools = [
                    {
                        "name": fq_tool_name(st.name, item["raw_name"]),
                        "raw_name": item["raw_name"],
                        "description": item.get("description", ""),
                        "dangerous": bool(item.get("dangerous", True)),
                        "enabled": fq_tool_name(st.name, item["raw_name"]) in registered_fq,
                    }
                    for item in st.all_tools
                ]
            else:
                # 没有发现记录（连接失败/未启动）时退回注册表里实际可见的工具
                tools = [
                    {
                        "name": t.name,
                        "raw_name": t.name,
                        "description": t.description,
                        "dangerous": bool(t.dangerous),
                        "enabled": True,
                    }
                    for t in registry_tools
                ]
            servers.append({
                "name": st.name,
                "command": st.command,
                "args": list(st.args),
                "transport": st.transport,
                "url": st.url,
                "status": st.status,
                "error": st.error,
                "server_info": dict(st.server_info),
                "tools": tools,
                "tool_count": sum(1 for t in tools if t["enabled"]),
                "tool_total": len(tools),
                "started_ms": st.started_ms,
                "attempts": st.attempts,
                "stderr_tail": list(st.stderr_tail),
                "logs": list(self._logs.get(st.name) or []),
            })
        return {"servers": servers, "cancel_stats": cancel}

    def test_spec(self, spec: dict) -> dict:
        """临时起一个连接器做试连：握手、列工具、立刻关闭，不写注册表。"""
        name = str(spec.get("name") or "test")
        cwd = spec.get("cwd") or self.root
        if not os.path.isabs(cwd):
            cwd = os.path.join(self.root, cwd)
        client = self._make_client(name, spec, cwd)
        try:
            client.start()
            tools = [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                }
                for t in client.list_tools()
            ]
            return {
                "ok": True,
                "server_info": client.server_info,
                "tools": tools,
                "stderr": client.stderr_tail(),
            }
        except Exception as e:  # noqa: BLE001 - 直接把原因回给界面
            return {
                "ok": False,
                "error": str(e),
                "stderr": client.stderr_tail(),
            }
        finally:
            client.stop()

    def cancel_stats(self) -> dict[str, int]:
        """汇总所有连接的取消相关统计（界面用来显示「通知真的发出去了吗」）。"""
        keys = ("requests", "cancellations", "late_responses", "timeouts")
        total = dict.fromkeys(keys, 0)
        for c in self.clients.values():
            for k in keys:
                total[k] += c.stats.get(k, 0)
        return total
