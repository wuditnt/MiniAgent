"""
Web UI 后端：把 Agent 的 9 个 UI 回调翻译成 SSE 事件流，浏览器即另一块「屏幕」。

设计要点（对应 docs/WEBUI_ANTDX_PLAN.md）：
  · 零依赖：stdlib http.server（ThreadingHTTPServer）+ 手写 SSE 帧
  · 双模式 = 每消息定制工具表：chat 关联网 → 空注册表；chat 开联网 → {web_search}；
    agent → 全量 REGISTRY。Agent.run() 全程走 self.registry（schemas/get/call），
    所以逐消息换 agent.registry 即可，核心代码一行不改。
  · 一个会话一个 Agent（保住消息历史与用量统计）；同一会话同时只允许一个任务
  · 危险确认：ask_confirm 回调推 confirm_request 事件后，在工作线程上
    Event.wait(120) 挂起；POST /api/confirm 置位事件返回用户选择
  · 取消：POST /api/stop → agent.interrupt()，复用 core/cancel 全链路
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core._paths import ROOT
from core import model_config
from core import settings
from core import workspace_config
from core.ui import brief

# ---------------------------------------------------------------- 常量

CONFIRM_TIMEOUT = 120          # 危险确认等待上限（秒），超时视为拒绝
PUMP_IDLE = 15                 # SSE 空闲心跳间隔（秒）
HISTORY_RESULT_LIMIT = 4000    # tool_end 事件里结果文本的传输上限
MAX_BODY = 1024 * 1024         # 请求体上限 1MB

_DIST = os.path.join(ROOT, "web", "dist")
_SESSIONS_DIR = os.path.join(ROOT, "web_data", "sessions")  # 会话持久化目录

# 原生文件夹选择框一次只允许弹一个（多个 Tk 实例并发会互相干扰）
_PICK_LOCK = threading.Lock()


def pick_folder_dialog() -> str | None:
    """弹出原生「选择文件夹」对话框，返回所选目录；用户取消返回 None。

    为什么由后端弹框而不是用浏览器的文件选择器？
      浏览器出于安全考虑不暴露真实绝对路径（只会给 C:\\fakepath\\...），
      而「工作区路径」必须是本机真实目录 —— 所以只能由本机进程弹原生框。
      tkinter 是标准库，符合零依赖约束。

    Tk 对象必须在创建它的线程里使用与销毁，因此自起一个一次性线程完成；
    本机部署（127.0.0.1）下后端与浏览器同机，弹出的就是用户面前的桌面。
    """
    def _ask(result: dict) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)  # 确保弹到浏览器窗口前面
                path = filedialog.askdirectory(parent=root, title="选择工作区文件夹")
            finally:
                root.destroy()
            result["path"] = path or None
        except Exception as e:  # noqa: BLE001 —— 缺 tkinter / 无桌面环境等
            result["error"] = str(e)

    if not _PICK_LOCK.acquire(blocking=False):
        raise RuntimeError("已有一个文件夹选择框正在等待操作，请先完成或取消它")
    try:
        result: dict = {}
        t = threading.Thread(target=_ask, args=(result,), daemon=True)
        t.start()
        t.join()  # 阻塞本请求线程直到用户选完 / 取消（ThreadingHTTPServer 各请求独立线程）
        if "error" in result:
            raise RuntimeError(f"无法打开文件夹选择框：{result['error']}")
        return result.get("path")
    finally:
        _PICK_LOCK.release()

# 通知文本里的 rich 颜色标记，网页上看不懂，剥掉
_RICH_TAG = re.compile(
    r"\[/?(?:yellow|dim|green|red|cyan|blue|magenta|bold|italic|white|black)\]"
)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


# ---------------------------------------------------------------- 会话

class _Session:
    """一个浏览器会话 = 一个 Agent + 一条事件队列 + 一个确认桥。

    Agent 在首次对话时才创建（构造轻，但避免无谓的初始化）；
    每条消息按 mode/web 重建工具表后换上，历史与统计自然延续。
    """

    def __init__(self, sid: str, server: "WebServer") -> None:
        self.id = sid
        self.server = server
        self.agent = None                     # 懒创建（首次对话）
        self.mode = "agent"
        self.web = False
        self.queue: queue.Queue | None = None
        self.busy = False                     # run_lock 的可观察副本
        self.run_lock = threading.Lock()
        self.confirm_lock = threading.Lock()
        self.pending_confirm: tuple[str, threading.Event, dict] | None = None
        # 持久化字段
        self.title = "新对话"
        self.title_custom = False            # 标题是否被手动改过（改过则不再按首条消息自动生成）
        self.workspace_id = workspace_config.DEFAULT_ID   # 归属工作区（决定沙箱根）
        now = time.time()
        self.created_at = now
        self.updated_at = now
        self._loaded = False                 # 是否已从磁盘灌入过历史

    def ensure_agent(self):
        if self.agent is None:
            from core.agent import Agent

            from core.registry import REGISTRY

            self.agent = Agent(
                model=self.server.model,
                system_prompt=self.server.system_prompt,
                max_turns=self.server.max_turns,
                verbose=False,
                registry=REGISTRY,        # 每条消息会按模式重建
                ui=None,
                approve=True,             # 网页端危险工具永远走确认弹窗
            )
            self.agent.ui = WebUI(self)
            self._inject_history()          # 从磁盘恢复历史消息
        return self.agent

    def _inject_history(self) -> None:
        """首次创建 Agent 时，把磁盘上的 user/assistant 消息灌入。"""
        if self._loaded or self.agent is None:
            return
        data = _load_session_file(self.id)
        if data:
            self.title = data.get("title") or self.title
            self.title_custom = bool(data.get("title_custom", False))
            self.workspace_id = data.get("workspace_id") or self.workspace_id
            self.created_at = data.get("created_at") or self.created_at
            self.updated_at = data.get("updated_at") or self.updated_at
            for m in data.get("messages", []):
                role, content = m.get("role"), m.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content:
                    self.agent.messages.append({"role": role, "content": content})
        self._loaded = True

    def save(self) -> None:
        """把当前会话（标题 + user/assistant 消息）写回磁盘。"""
        if self.agent is None:
            return
        messages = []
        for m in self.agent.messages[1:]:   # [0] 是 system
            role, content = m.get("role"), m.get("content")
            if role == "user" and isinstance(content, str) and content:
                messages.append({"role": "user", "content": content})
            elif role == "assistant" and isinstance(content, str) and content:
                messages.append({"role": "assistant", "content": content})
        # 标题 = 第一条 user 消息前 20 字；用户手动改过标题则保留
        if messages and not self.title_custom:
            first = next((m["content"] for m in messages if m["role"] == "user"), "")
            self.title = first[:20].replace("\n", " ") or "新对话"
        self.updated_at = time.time()
        data = {
            "id": self.id,
            "title": self.title,
            "title_custom": self.title_custom,
            "workspace_id": self.workspace_id,
            "messages": messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        _save_session_file(self.id, data)

    def answer_confirm(self, cid: str, approve: bool) -> bool:
        """浏览器对 confirm_request 的应答。返回是否真的匹配到了待确认项。"""
        with self.confirm_lock:
            pending = self.pending_confirm
            if not pending or pending[0] != cid:
                return False
            pending[2]["approve"] = bool(approve)
            pending[1].set()
        return True

    def deny_pending_confirm(self) -> None:
        """以「拒绝」解除正在等待的确认（停止任务 / 浏览器断连时调用）。

        ask_confirm 挂在工作线程的 Event.wait 上，取消令牌叫不醒它；
        不主动放行的话，worker 会一直卡到 120s 超时。
        """
        with self.confirm_lock:
            pending = self.pending_confirm
            if pending:
                pending[2]["approve"] = False
                pending[1].set()


class WebUI:
    """Agent 的 UI 实现：9 个回调全部翻译成事件，推入当前会话队列。

    方法签名与 core/ui.py 的约定一致；agent 通过 _ui_call 反射调用，
    少实现某个方法也只是「网页上看不到对应信息」，不会崩。
    """

    def __init__(self, session: _Session) -> None:
        self.session = session
        # tool_end 只有 (turn, tool, result, sec)，没有下标 ——
        # 靠「每个工具名的待完成 id 队列」把 start/end 配对；
        # 同名工具在同批并行时完成顺序可能与开始顺序不同，这是 v1 接受的误差。
        self._pending_ids: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def _push(self, event: dict) -> None:
        q = self.session.queue
        if q is not None:
            q.put(event)

    # ---------------------------------------------------------- 9 个回调

    def notice(self, text: str) -> None:
        self._push({"type": "notice", "text": _RICH_TAG.sub("", text or "")})

    def turn_start(self, turn: int) -> None:
        self._push({"type": "turn", "turn": turn, "mode": self.session.mode})

    def llm_delta(self, turn: int, text: str, kind: str = "content") -> bool:
        if not text:
            return False
        self._push({"type": "delta", "text": text, "kind": kind})
        # 返回 True 仅限正文：agent 靠它判断「回答已经上屏过」，网页上确实上屏了
        return kind == "content"

    def llm_done(self, turn: int, latency: float, streamed: bool = False) -> None:
        self._push({"type": "llm_done", "sec": round(latency, 2), "streamed": streamed})

    def tools_start(self, turn: int, items: list) -> None:
        """并行批次：逐个发 tool_start（ThoughtChain 需要 pending 节点）。"""
        with self._lock:
            for tool, args in items:
                tid = f"{turn}-{tool.name}-{uuid.uuid4().hex[:6]}"
                self._pending_ids.setdefault(tool.name, []).append(tid)
                self._push({"type": "tool_start", "id": tid, "name": tool.name,
                            "args": _brief_args(args)})

    def tool_start(self, turn: int, tool, args: dict) -> None:
        with self._lock:
            tid = f"{turn}-{tool.name}-{uuid.uuid4().hex[:6]}"
            self._pending_ids.setdefault(tool.name, []).append(tid)
        self._push({"type": "tool_start", "id": tid, "name": tool.name,
                    "args": _brief_args(args)})

    def tool_end(self, turn: int, tool, result: str, seconds: float) -> None:
        with self._lock:
            ids = self._pending_ids.get(tool.name)
            tid = ids.pop(0) if ids else f"{turn}-{tool.name}-?"
            if ids is not None and not ids:
                self._pending_ids.pop(tool.name, None)
        self._push({
            "type": "tool_end", "id": tid,
            "ok": not result.startswith(("[error]", "[denied]", "[cancelled]")),
            "sec": round(seconds, 2),
            "result": result[:HISTORY_RESULT_LIMIT],
        })

    def ask_confirm(self, tool, args: dict) -> bool:
        """危险确认桥：推事件 → Event.wait 挂起工作线程 → /api/confirm 置位。"""
        session = self.session
        cid = uuid.uuid4().hex[:12]
        event = threading.Event()
        holder = {"approve": False}
        with session.confirm_lock:
            session.pending_confirm = (cid, event, holder)
        self._push({"type": "confirm_request", "id": cid, "tool": tool.name,
                    "args": _brief_args(args)})
        got = event.wait(CONFIRM_TIMEOUT)
        with session.confirm_lock:
            session.pending_confirm = None
        return got and holder["approve"]

    def cancelled(self, reason: str = "") -> bool:
        self._push({"type": "cancelled", "reason": reason or "任务被取消"})
        return True

    def flush_stream(self) -> None:
        pass    # 网页没有「行」的概念，无需收尾


def _brief_args(args: dict) -> dict:
    """参数过墙：每值截断，防止超大参数把事件流撑爆。"""
    return {k: brief(v, 300) for k, v in (args or {}).items()}


def _session_registry(mode: str, web: bool):
    """按行为矩阵克隆一份会话工具表（见方案 §3.1）。

    Tool 对象本身无状态，跨注册表共享引用是安全的。
    """
    from core.registry import REGISTRY, ToolRegistry

    reg = ToolRegistry()
    for t in REGISTRY.all():
        if mode == "chat" and not (web and t.name == "web_search"):
            continue
        reg.add(t)
    return reg


def _search_backend() -> str:
    """探测当前搜索后端，供 /api/state 报告（工具未加载时返回 unknown）。"""
    try:
        import plugins.web_search as ws

        return ws._backend()
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------- 会话持久化

def _session_file(sid: str) -> str:
    return os.path.join(_SESSIONS_DIR, f"{sid}.json")


def _save_session_file(sid: str, data: dict) -> None:
    """原子写：先写临时文件再 rename，避免进程被杀导致半截 JSON。"""
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    path = _session_file(sid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_session_file(sid: str) -> dict | None:
    path = _session_file(sid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _delete_session_file(sid: str) -> bool:
    path = _session_file(sid)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _list_session_files() -> list[dict]:
    """扫描 sessions 目录，返回所有会话的元信息（不含 messages，避免一次读太多）。"""
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    items: list[dict] = []
    for name in os.listdir(_SESSIONS_DIR):
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        sid = name[:-5]
        data = _load_session_file(sid)
        if not data:
            continue
        msgs = data.get("messages", [])
        items.append({
            "id": data.get("id") or sid,
            "title": data.get("title") or "新对话",
            "workspace_id": data.get("workspace_id") or workspace_config.DEFAULT_ID,
            "message_count": len(msgs),
            "created_at": data.get("created_at", 0),
            "updated_at": data.get("updated_at", 0),
        })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def _usage_delta(before: dict, after: dict) -> dict:
    """本轮用量 = after - before，供前端「用量」展示（负数按 0 处理）。"""
    keys = ("llm_calls", "tool_calls", "prompt_tokens", "completion_tokens", "total_tokens")
    return {k: max(0, int(after.get(k, 0)) - int(before.get(k, 0))) for k in keys}


def _snippet(text: str, pos: int, width: int = 60) -> str:
    """截取命中位置前后的一段上下文（两端加省略号）。"""
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _match_terms(title: str, msgs: list[str], terms: list[str]):
    """多词 AND 匹配：所有词都出现在标题或正文里才算命中。

    返回 (hit_index, snippet)：
      · 未命中        → None
      · 仅标题命中    → (-1, "")          前端只需高亮标题
      · 正文命中      → (消息下标, 片段)   前端可滚动定位到第 N 条
    """
    hay = (title + "\n" + "\n".join(msgs)).lower()
    if not all(t in hay for t in terms):
        return None
    best_i, best_hits, best_pos = -1, 0, 0
    for i, text in enumerate(msgs):
        tl = text.lower()
        present = [t for t in terms if t in tl]
        if len(present) > best_hits:
            best_i, best_hits = i, len(present)
            best_pos = min(tl.find(t) for t in present)
    if best_i < 0:
        return -1, ""
    return best_i, _snippet(msgs[best_i], best_pos)


# ---------------------------------------------------------------- HTTP 服务

class WebServer:
    """HTTP 服务外壳：装配 handler、会话表与静态托管。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        model: str | None = None,
        system_prompt: str = "",
        max_turns: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.model = model
        self.system_prompt = system_prompt
        self.max_turns = max_turns      # None = 交给通用配置决定
        self.sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None

    # ---------------------------------------------------------- 会话

    def session(self, sid: str) -> _Session:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                s = _Session(sid, self)
                self.sessions[sid] = s
            return s

    def reset_agents(self) -> None:
        """切换模型后让所有会话的 Agent 下次对话时重建。

        Agent 的 model 与底层 OpenAI 客户端都是构造时定型的，换模型必须重建；
        历史消息已落盘，_loaded 归位后重建时会重新灌入，对话内容不丢。
        """
        with self._lock:
            for s in self.sessions.values():
                s.agent = None
                s._loaded = False

    # ---------------------------------------------------------- 运行

    def serve_forever(self) -> None:
        server = self

        class _Handler(_RequestHandler):
            web = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        print(f"Web UI 已启动：http://{self.host}:{self.port}")
        print("生产模式需先构建前端：cd web && npm run build（开发模式见 README）")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n正在关闭 Web UI …")
        finally:
            self._httpd.server_close()

    def shutdown(self) -> None:
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()


class _RequestHandler(BaseHTTPRequestHandler):
    """路由：/api/* + 静态 dist。所有 JSON 交互都在这里，SSE 只属于 /api/chat。"""

    web: WebServer = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    server_version = "mini-agent-web/0.1"

    # ---------------------------------------------------------- 基础设施

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass    # 静默：本地单用户，逐请求日志只会刷屏

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse_frame(self, event: dict) -> bool:
        """写一帧 SSE。返回 False 表示客户端已断开。"""
        try:
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return False

    # ---------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/state":
            self._handle_state()
        elif route == "/api/history":
            self._handle_history(parse_qs(parsed.query))
        elif route == "/api/sessions":
            self._handle_sessions_list(parse_qs(parsed.query))
        elif route == "/api/workspaces":
            self._handle_workspaces_list()
        elif route == "/api/models":
            self._handle_models_list()
        elif route == "/api/settings":
            self._handle_settings_get()
        else:
            self._serve_static(route)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/chat":
            self._handle_chat()
        elif route == "/api/confirm":
            self._handle_confirm()
        elif route == "/api/stop":
            self._handle_stop()
        elif route == "/api/sessions":
            self._handle_session_create()
        elif route == "/api/workspaces/pick":
            self._handle_workspace_pick()
        elif route == "/api/workspaces":
            self._handle_workspace_create()
        elif route == "/api/models":
            self._handle_model_create()
        elif route == "/api/models/test":
            self._handle_model_test_raw()
        else:
            m = re.match(r"^/api/models/([^/]+)/(test|activate)$", route)
            if m:
                self._handle_model_action(m.group(1), m.group(2))
            else:
                self._fail(404, f"未知接口：{route}")

    def do_PUT(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/settings":
            self._handle_settings_put()
            return
        m = re.match(r"^/api/sessions/([^/]+)$", route)
        if m:
            self._handle_session_update(m.group(1))
            return
        m = re.match(r"^/api/workspaces/([^/]+)$", route)
        if m:
            self._handle_workspace_update(m.group(1))
            return
        m = re.match(r"^/api/models/([^/]+)$", route)
        if m:
            self._handle_model_update(m.group(1))
            return
        self._fail(404, f"未知接口：{route}")

    def do_DELETE(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        m = re.match(r"^/api/sessions/([^/]+)$", route)
        if m:
            self._handle_session_delete(m.group(1))
            return
        m = re.match(r"^/api/workspaces/([^/]+)$", route)
        if m:
            self._handle_workspace_delete(m.group(1))
            return
        m = re.match(r"^/api/models/([^/]+)$", route)
        if m:
            self._handle_model_delete(m.group(1))
            return
        self._fail(404, f"未知接口：{route}")

    # ---------------------------------------------------------- API 实现

    def _handle_state(self) -> None:
        sessions = []
        with self.web._lock:
            items = list(self.web.sessions.items())
        for sid, s in items:
            sessions.append({"id": sid, "busy": s.busy})
        cfg = model_config.resolve_active()
        self._json({
            "model": cfg.get("model") or self.web.model or "",
            "model_name": cfg.get("name") or "",
            "workspace": self._workspace(),
            "tools": self._tool_count(),
            "search_backend": _search_backend(),
            "confirm_timeout": CONFIRM_TIMEOUT,
            "sessions": sessions,
        })

    @staticmethod
    def _workspace() -> str:
        try:
            from core.sandbox import workspace

            return workspace()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _tool_count() -> int:
        try:
            from core.registry import REGISTRY

            return len(REGISTRY)
        except Exception:  # noqa: BLE001
            return 0

    def _handle_history(self, params: dict) -> None:
        """回放会话历史。

        优先用内存里的 Agent（含本轮最新状态）；本进程还没跑过该会话时
        退回磁盘文件 —— 否则刷新页面 / 重启服务后，老会话会显示为空。
        """
        sid = (params.get("session") or ["default"])[0]
        session = self.web.session(sid)
        if session.agent is not None:
            src = session.agent.messages[1:]          # [0] 是 system
        else:
            src = (_load_session_file(sid) or {}).get("messages", [])
        messages: list[dict] = []
        for m in src:
            role, content = m.get("role"), m.get("content")
            if role == "user" and isinstance(content, str) and content:
                messages.append({"role": "user", "content": content})
            elif role == "assistant" and isinstance(content, str) and content:
                messages.append({"role": "assistant", "content": content})
            # tool 消息与无正文的 tool_calls 消息不回放：轨迹只在实时事件里展示
        self._json({"session": sid, "messages": messages})

    def _handle_sessions_list(self, params: dict) -> None:
        """列出会话。q 支持多词 AND（标题或正文命中全部词）；workspace 按归属过滤。

        命中正文时额外返回 hit_index（命中第几条消息，供前端跳转定位）
        与 snippet（命中处上下文，供前端高亮）。
        """
        q = (params.get("q") or [""])[0].strip()
        wid = (params.get("workspace") or [""])[0].strip()
        items = _list_session_files()
        if wid:
            items = [it for it in items if it.get("workspace_id") == wid]
        terms = [t.lower() for t in q.split() if t]
        if terms:
            filtered = []
            for it in items:
                data = _load_session_file(it["id"])
                if not data:
                    continue
                msgs = [str(m.get("content") or "") for m in data.get("messages", [])]
                matched = _match_terms(it["title"], msgs, terms)
                if matched is None:
                    continue
                hit_index, snippet = matched
                filtered.append({**it, "hit_index": hit_index, "snippet": snippet})
            items = filtered
        self._json({"sessions": items})

    def _handle_session_create(self) -> None:
        """新建一个空会话，归属指定工作区（缺省用默认工作区）。"""
        body = self._read_body() or {}
        wid = str(body.get("workspace") or workspace_config.DEFAULT_ID)[:64]
        if workspace_config.get_workspace(wid) is None:
            wid = workspace_config.DEFAULT_ID
        sid = uuid.uuid4().hex
        now = time.time()
        data = {"id": sid, "title": "新对话", "workspace_id": wid, "messages": [],
                "created_at": now, "updated_at": now}
        _save_session_file(sid, data)
        # 同时在内存里登记一个空会话，并把归属工作区带上
        session = self.web.session(sid)
        session.workspace_id = wid
        self._json({"id": sid, "title": "新对话", "workspace_id": wid})

    def _handle_session_delete(self, sid: str) -> None:
        """删除一个会话（磁盘文件 + 内存）。"""
        sid = sid[:64]
        with self.web._lock:
            self.web.sessions.pop(sid, None)
        ok = _delete_session_file(sid)
        if ok:
            self._json({"ok": True})
        else:
            self._fail(404, "会话不存在")

    def _handle_session_update(self, sid: str) -> None:
        """修改会话标题：{title}。改过之后不再按首条消息自动生成标题。"""
        sid = sid[:64]
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{title}）")
            return
        title = str(body.get("title") or "").strip()
        if not title:
            self._fail(400, "标题不能为空")
            return
        title = title[:60]
        data = _load_session_file(sid)
        if data is None:
            self._fail(404, "会话不存在")
            return
        data["title"] = title
        data["title_custom"] = True
        _save_session_file(sid, data)
        # 内存里的会话也同步（否则后续追加消息 save() 时会被旧值覆盖）
        with self.web._lock:
            s = self.web.sessions.get(sid)
        if s is not None:
            s.title = title
            s.title_custom = True
        self._json({"id": sid, "title": title})

    # ------------------------------------------------ 工作区 API

    def _handle_workspaces_list(self) -> None:
        """工作区列表 + 每个工作区下的会话数 + 路径是否仍存在。"""
        counts: dict[str, int] = {}
        for it in _list_session_files():
            wid = it.get("workspace_id") or workspace_config.DEFAULT_ID
            counts[wid] = counts.get(wid, 0) + 1
        items = []
        for w in workspace_config.list_workspaces():
            items.append({
                **w,
                "session_count": counts.get(w["id"], 0),
                "exists": os.path.isdir(w["path"]),
            })
        self._json({"workspaces": items})

    def _handle_workspace_pick(self) -> None:
        """弹原生文件夹选择框：返回 {path}；用户取消返回 {path: null}。"""
        try:
            path = pick_folder_dialog()
        except RuntimeError as e:
            self._fail(500, str(e))
            return
        self._json({"path": path})

    def _handle_workspace_create(self) -> None:
        """新建工作区：{name, path}，path 必须是已存在的目录。"""
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{name, path}）")
            return
        try:
            item = workspace_config.create_workspace(
                str(body.get("name") or ""), str(body.get("path") or "")
            )
        except ValueError as e:
            self._fail(400, str(e))
            return
        self._json({**item, "session_count": 0, "exists": True}, 201)

    def _handle_workspace_update(self, wid: str) -> None:
        """改名 / 改路径：字段缺省表示不改。"""
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{name?, path?}）")
            return
        try:
            item = workspace_config.update_workspace(
                wid[:64],
                name=body.get("name") if "name" in body else None,
                path=body.get("path") if "path" in body else None,
            )
        except ValueError as e:
            self._fail(400, str(e))
            return
        self._json(item)

    def _handle_workspace_delete(self, wid: str) -> None:
        """删除工作区：默认工作区不可删；其下还有会话时禁止删除。"""
        wid = wid[:64]
        if wid == workspace_config.DEFAULT_ID:
            self._fail(409, "默认工作区不可删除")
            return
        if workspace_config.get_workspace(wid) is None:
            self._fail(404, "工作区不存在")
            return
        n = sum(
            1 for it in _list_session_files()
            if (it.get("workspace_id") or workspace_config.DEFAULT_ID) == wid
        )
        if n:
            self._fail(409, f"该工作区下还有 {n} 个对话，请先删除后再移除工作区")
            return
        try:
            workspace_config.delete_workspace(wid)
        except ValueError as e:
            self._fail(400, str(e))
            return
        self._json({"ok": True})

    # ------------------------------------------------ 模型 API

    def _handle_models_list(self) -> None:
        """模型配置列表（key 已脱敏）+ 内置模板 + 当前选中。"""
        self._json({
            "providers": model_config.list_public(),
            "templates": model_config.templates(),
            "active": model_config.active_id(),
        })

    def _handle_model_create(self) -> None:
        """新增一份「服务商 + 模型」配置。"""
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{provider, name, base_url, api_key, model}）")
            return
        try:
            item = model_config.create_entry(
                str(body.get("provider") or "custom"),
                str(body.get("name") or ""),
                str(body.get("base_url") or ""),
                str(body.get("api_key") or ""),
                str(body.get("model") or ""),
            )
        except ValueError as e:
            self._fail(400, str(e))
            return
        self._json({"id": item["id"]}, 201)

    def _handle_model_update(self, pid: str) -> None:
        """改名/改地址/改模型/换 key（api_key 传空串表示保持原值）。"""
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON")
            return
        try:
            model_config.update_entry(
                pid[:64],
                name=body.get("name") if "name" in body else None,
                base_url=body.get("base_url") if "base_url" in body else None,
                api_key=body.get("api_key") if "api_key" in body else None,
                model=body.get("model") if "model" in body else None,
            )
        except ValueError as e:
            self._fail(400, str(e))
            return
        self.web.reset_agents()
        self._json({"ok": True})

    def _handle_model_delete(self, pid: str) -> None:
        try:
            model_config.delete_entry(pid[:64])
        except ValueError as e:
            self._fail(400, str(e))
            return
        self.web.reset_agents()
        self._json({"ok": True})

    def _handle_model_test_raw(self) -> None:
        """按未保存的表单值做连接测试（{base_url, api_key}）。"""
        body = self._read_body() or {}
        self._json(model_config.test_values(
            str(body.get("base_url") or ""), str(body.get("api_key") or "")
        ))

    def _handle_model_action(self, pid: str, action: str) -> None:
        """test = 连接测试；activate = 设为当前。"""
        pid = pid[:64]
        if action == "test":
            self._json(model_config.test_entry(pid))
            return
        try:
            model_config.set_active(pid)
        except ValueError as e:
            self._fail(400, str(e))
            return
        self.web.reset_agents()          # 换模型 → 已有会话 Agent 重建
        self._json({"ok": True, "model": model_config.resolve_active()})

    # ------------------------------------------------ 通用配置 API

    def _handle_settings_get(self) -> None:
        """通用配置：当前有效值 + 内置默认值（前端用来做「恢复默认」）。"""
        self._json({"values": settings.get_all(), "defaults": settings.defaults()})

    def _handle_settings_put(self) -> None:
        """更新通用配置：{...字段} 或 {"reset": true}。

        改完立刻生效：影响进程全局的项同步到运行时，其余靠重建会话 Agent 落地。
        """
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON")
            return
        reset = bool(body.pop("reset", False))
        try:
            if reset:
                settings.reset()
            else:
                settings.update(body)
        except ValueError as e:
            self._fail(400, str(e))
            return
        settings.apply_to_runtime()
        self.web.reset_agents()
        self._json({"values": settings.get_all(), "defaults": settings.defaults()})

    def _handle_chat(self) -> None:
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{message, mode, web}）")
            return
        message = str(body.get("message") or "").strip()
        if not message:
            self._fail(400, "message 不能为空")
            return
        mode = body.get("mode") if body.get("mode") in ("chat", "agent") else "agent"
        web = bool(body.get("web"))
        sid = str(body.get("session") or "default")[:64]

        session = self.web.session(sid)
        if not session.run_lock.acquire(blocking=False):
            self._fail(409, "该会话已有任务在执行，请先停止或等待完成")
            return

        agent = session.ensure_agent()

        # 还没开始过的会话，允许前端指定工作区（缺省留在默认工作区）；
        # 一旦有了消息就不再改 —— 换沙箱根会让「之前写的文件在哪」变得难以理解
        #
        # 必须放在 ensure_agent() 之后：_inject_history() 会从磁盘把 workspace_id
        # 恢复进来，先设的话会被它覆盖回去。
        ws = str(body.get("workspace") or "").strip()
        if ws and workspace_config.get_workspace(ws) and not agent.messages[1:]:
            session.workspace_id = ws

        session.mode, session.web = mode, web
        session.busy = True
        q: queue.Queue = queue.Queue()
        session.queue = q

        worker = threading.Thread(
            target=self._run_worker,
            args=(session, agent, message, mode, web, q),
            daemon=True,
            name=f"web-chat-{sid}",
        )
        worker.start()

        self._sse_headers()
        self.close_connection = True
        alive = True
        try:
            while True:
                try:
                    event = q.get(timeout=PUMP_IDLE)
                except queue.Empty:
                    # SSE 注释帧当心跳（客户端不会把它当事件解析），顺带探测断连
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                        alive = False
                        agent.interrupt("浏览器断开了连接")
                        break
                    continue
                if not self._sse_frame(event):
                    alive = False
                    agent.interrupt("浏览器断开了连接")
                    session.deny_pending_confirm()
                    break
                if event.get("type") in ("done", "error", "cancelled"):
                    break
        finally:
            session.busy = False
            session.queue = None
            session.run_lock.release()
            if not alive:
                # 工作线程很快会被取消；把队列排干，避免事件滞留
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass

    @staticmethod
    def _run_worker(
        session: _Session, agent, message: str, mode: str, web: bool, q: queue.Queue
    ) -> None:
        """工作线程：换工具表 → agent.run() → 终态事件。

        本轮用量 = run 前后的 usage 差值（agent.usage 是整会话累计的，
        前端要的是「这一条回答花了多少」，所以必须取增量）。
        """
        from core.cancel import Cancelled
        from core.sandbox import set_workspace

        t0 = time.time()
        before = dict(agent.usage)
        try:
            # 把沙箱根切到本会话所属工作区，之后工具的相对路径都落在这里
            set_workspace(workspace_config.resolve_path(session.workspace_id))
            agent.registry = _session_registry(mode, web)
            answer = agent.run(message)
            # 先落盘再发终态：前端收到 done 就会刷新会话列表，
            # 反过来的话文件还没写完，新会话/新标题会漏掉一拍
            session.save()
            q.put({"type": "done", "answer": answer,
                   "sec": round(time.time() - t0, 1),
                   "usage": _usage_delta(before, agent.usage)})
        except Cancelled as e:
            # ui.cancelled 已发过 cancelled 事件（终态，泵会停）；没发过才补发
            if not getattr(agent, "last_cancel_notice_shown", False):
                q.put({"type": "cancelled", "reason": str(e),
                       "usage": _usage_delta(before, agent.usage)})
        except Exception as e:  # noqa: BLE001 —— 任何异常都转成 error 事件
            q.put({"type": "error", "message": f"{type(e).__name__}: {e}",
                   "usage": _usage_delta(before, agent.usage)})

    def _handle_confirm(self) -> None:
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{id, approve}）")
            return
        sid = str(body.get("session") or "default")[:64]
        session = self.web.session(sid)
        cid = str(body.get("id") or "")
        if not cid:
            self._fail(400, "id 不能为空")
            return
        matched = session.answer_confirm(cid, bool(body.get("approve")))
        self._json({"ok": matched})

    def _handle_stop(self) -> None:
        body = self._read_body() or {}
        sid = str(body.get("session") or "default")[:64]
        session = self.web.session(sid)
        agent = session.agent
        if agent is None or not session.busy:
            self._json({"ok": False, "reason": "当前没有正在运行的任务"})
            return
        agent.interrupt("用户在网页上停止了任务")
        session.deny_pending_confirm()   # 若卡在确认弹窗上，拒绝以解除阻塞
        self._json({"ok": True})

    # ---------------------------------------------------------- 静态托管

    def _serve_static(self, route: str) -> None:
        rel = route.lstrip("/") or "index.html"
        path = os.path.realpath(os.path.join(_DIST, rel))
        inside = path == os.path.realpath(_DIST) or path.startswith(os.path.realpath(_DIST) + os.sep)
        if not os.path.isfile(path) or not inside:
            if os.path.isfile(os.path.join(_DIST, "index.html")):
                path = os.path.join(_DIST, "index.html")   # SPA 路由兜底
            else:
                self._no_dist()
                return
        ext = os.path.splitext(path)[1].lower()
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._fail(404, "文件读取失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if ext in (".html", ".json") else "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _no_dist(self) -> None:
        """dist 未构建时的引导页：说明如何跑起来，而不是让浏览器白屏。"""
        body = (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>mini-agent Web UI</title>"
            "<body style='font-family:system-ui;max-width:640px;margin:80px auto;"
            "line-height:1.8;color:#333'>"
            "<h2>mini-agent Web UI</h2>"
            "<p>后端已正常运行，但前端尚未构建（缺少 <code>web/dist</code>）。</p>"
            "<p>开发模式：<code>cd web && npm install && npm run dev</code>"
            "（Vite 端口 5173，API 自动代理到本服务）</p>"
            "<p>生产模式：<code>cd web && npm run build</code>，"
            "然后刷新本页即可。</p>"
            "<p>接口自检：<a href='/api/state'>/api/state</a></p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    model: str | None = None,
    system_prompt: str = "",
    max_turns: int | None = None,
) -> None:
    """便捷入口：构建服务并阻塞运行（main.py 的 --web 走这里）。"""
    settings.apply_to_runtime()      # 通用配置里的运行时项（工具输出上限）先落地
    WebServer(
        host=host, port=port, model=model,
        system_prompt=system_prompt, max_turns=max_turns,
    ).serve_forever()
