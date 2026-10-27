"""
Web UI 后端：把 Agent 的 9 个 UI 回调翻译成 SSE 事件流，浏览器即另一块「屏幕」。

设计要点（对应 docs/WEBUI_ANTDX_PLAN.md）：
  · 零依赖：stdlib http.server（ThreadingHTTPServer）+ 手写 SSE 帧
  · 双模式 = 每消息定制工具表：chat 关联网 → 空注册表；chat 开联网 → {web_search}；
    agent → 全量 REGISTRY（关联网时剔除 web_search）。Agent.run() 全程走
    self.registry（schemas/get/call），所以逐消息换 agent.registry 即可，核心代码一行不改。
  · 一个会话一个 Agent（保住消息历史与用量统计）；同一会话同时只允许一个任务
  · 危险确认：ask_confirm 回调推 confirm_request 事件后，在工作线程上
    Event.wait(120) 挂起；POST /api/confirm 置位事件返回用户选择
  · 取消：POST /api/stop → agent.interrupt()，复用 core/cancel 全链路
"""

from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import signal
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from core._paths import ROOT, data_dir
from core import auth
from core import events
from core import logging_setup
from core import model_config
from core import settings
from core import workspace_config
from core.images import ATTACH_NOTE
# brief 不再从 core.ui 导入：它会把 \n 换成 ⏎ 给控制台单行展示用，
# SSE 路径（confirm_request / tool 事件）需要保留真实换行让前端代码块渲染。

# ---------------------------------------------------------------- 常量

CONFIRM_TIMEOUT = 120          # 危险确认等待上限（秒），超时视为拒绝
PUMP_IDLE = 15                 # SSE 空闲心跳间隔（秒）
HISTORY_RESULT_LIMIT = 4000    # tool_end 事件里结果文本的传输上限
CONFIRM_ARG_LIMIT = 20000      # 危险确认弹窗参数上限：需完整展示脚本/代码，故放宽
MAX_BODY = 32 * 1024 * 1024    # 请求体上限 32MB（base64 附件膨胀约 1/3，实际文件上限约 24MB）
MAX_UPLOAD_BYTES = 24 * 1024 * 1024   # 单个附件解码后上限
MAX_ATTACHMENT_TEXT = 8000     # 文本附件注入消息的内容节选上限（字符）
ATTACH_DIR = ".attachments"    # 工作区内附件存放子目录

# 远程访问识别：判定依据是 TCP 对端地址 —— 回环地址视为本机，其余视为远程。
# 该标志仅用于 /api/state → 前端侧栏隐藏工作区管理入口（软性收窄）；
# 真正的访问控制由登录会话（core.auth）负责，与本标志无关。
_LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

#: 登录会话 Cookie 名。HttpOnly + SameSite=Lax，脚本读不到、跨站表单带不上。
_SESSION_COOKIE = "ma_session"

#: Host 头白名单：只认「本机回环 + 本机网卡 IP + 本机主机名 + 显式配置」。
#: 为什么要校验：DNS rebinding 会把攻击者的域名解析到 127.0.0.1，
#: 浏览器随后发出的请求会带上受害者的 Cookie —— 但 Host 是攻击者的域名。
#: 反向代理 / 自定义域名场景用环境变量 MINI_AGENT_ALLOWED_HOSTS 追加（逗号分隔）。
_ALLOWED_HOSTS_ENV = "MINI_AGENT_ALLOWED_HOSTS"
_ALLOWED_HOSTS_TTL = 60          # 网卡 IP 可能随 DHCP 变化，缓存 60 秒足够
_allowed_hosts_cache: tuple[float, frozenset[str]] = (0.0, frozenset())
_allowed_hosts_lock = threading.Lock()

#: --no-login 时用的占位身份（仅供调试与自动化测试，不写库）
_LOCAL_USER = {
    "id": 0, "username": "local", "display_name": "本机用户",
    "role": auth.ROLE_SUPERADMIN, "disabled": False,
    "must_change_password": False, "created_at": "", "last_login_at": None,
}

_DIST = os.path.join(ROOT, "web", "dist")
_SESSIONS_DIR = os.path.join(data_dir(), "sessions")  # 会话持久化目录

#: 静态资源目录可以用环境变量覆盖 —— 桌面版打包后 dist 不在 ROOT/web 下
_WEB_DIST_ENV = "MINI_AGENT_WEB_DIST"
_explicit_dist = os.environ.get(_WEB_DIST_ENV, "").strip().strip('"')
if _explicit_dist:
    _DIST = os.path.abspath(os.path.expanduser(_explicit_dist))

#: 进程启动时刻（/api/health 用它算 uptime）
_STARTED_AT = time.time()

# 原生文件夹选择框一次只允许弹一个（多个 Tk 实例并发会互相干扰）
_PICK_LOCK = threading.Lock()

_log = logging_setup.get_logger("app")


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
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".pdf": "application/pdf",
}

# 工作台文件浏览：只读、只允许工作区内
_TEXT_PREVIEW_BYTES = 20 * 1024 * 1024  # 文本最多读 20MB
_TEXT_PREVIEW_LINES = 5000              # 最多展示 5000 行
_TEXT_PREVIEW_CHARS = 4 * 1024 * 1024   # 展示字符上限（保护前端高亮）
_TEXT_LINE_MAX_CHARS = 4000             # 单行字符上限
_DIR_MAX_ENTRIES = 500                  # 目录最多返回 500 项
_RAW_MAX_BYTES = 20 * 1024 * 1024       # 原始字节预览（图片/PDF 等）上限 20MB
_HIDDEN_DIRS = frozenset({".memory", ".attachments"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"})

# 表格预览（CSV / TSV）：解析上限 + 单页窗口。
# 10M 个格子不可能一次性传给前端，所以按「行窗口 × 列窗口」分页返回，
# 解析时也只保留当前窗口，内存占用与总行数无关。
_TABLE_EXTS = frozenset({".csv", ".tsv"})
_TABLE_MAX_BYTES = 20 * 1024 * 1024
_TABLE_MAX_ROWS = 10000
_TABLE_MAX_COLS = 1000
_TABLE_PAGE_ROWS = 200
_TABLE_PAGE_COLS = 50

# 「按原始字节预览」的格式登记表：扩展名 → /api/workspace/file 返回的 kind。
# 新增一种格式只需两步：这里加一行（必要时补 _CONTENT_TYPES），
# 前端 components/preview/registry.tsx 里注册一个渲染组件；其余逻辑不用动。
_BINARY_PREVIEWS = {
    **{ext: "image" for ext in _IMAGE_EXTS},
    ".pdf": "pdf",
}

# 常见文本扩展名：统一按 text/plain 返回，具体高亮交给前端
for _ext in (
    ".md", ".markdown", ".csv", ".tsv", ".log", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".xml", ".py", ".ts", ".tsx", ".jsx", ".vue",
    ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".rb", ".php",
    ".bat", ".ps1", ".sh", ".sql",
):
    _CONTENT_TYPES.setdefault(_ext, "text/plain; charset=utf-8")


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
        self.approval_mode = "ask"            # 三档确认：ask / auto / full，逐轮生效
        self.queue: queue.Queue | None = None
        self.busy = False                     # run_lock 的可观察副本
        self.run_lock = threading.Lock()
        self.confirm_lock = threading.Lock()
        self.pending_confirm: tuple[str, threading.Event, dict] | None = None
        # ask_user 内联提问卡片的待答项：(cid, event, {"answer": str})
        self.pending_ask: tuple[str, threading.Event, dict] | None = None
        # 持久化字段
        self.title = "新对话"
        self.title_custom = False            # 标题是否被手动改过（改过则不再按首条消息自动生成）
        self.workspace_id = workspace_config.DEFAULT_ID   # 归属工作区（决定沙箱根）
        self.model_id: str | None = None     # 本会话的模型配置 id（None = 跟全局默认）
        self.reasoning: str | None = None    # 本会话的推理等级（None = 跟全局默认）
        self.web = False                     # 本会话的联网搜索开关（每个对话独立）
        now = time.time()
        self.created_at = now
        self.updated_at = now
        self._loaded = False                 # 是否已从磁盘灌入过历史

    def ensure_agent(self):
        if self.agent is None:
            from core.agent import Agent

            from core.registry import REGISTRY

            # 会话级模型：优先本会话保存的 model_id，无效/未存 → 回退全局 active
            cfg = model_config.resolve_model(self.model_id)
            self.agent = Agent(
                model=cfg["model"] or self.server.model,
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                reasoning_level=self.reasoning or model_config.reasoning_level(),
                reasoning_spec=model_config.reasoning_spec(cfg["provider"]),
                system_prompt=self.server.system_prompt,
                max_turns=self.server.max_turns,
                verbose=False,
                registry=REGISTRY,        # 每条消息会按模式重建
                ui=None,
                approval_mode="ask",      # 网页端默认「按需询问」；逐轮可由前端切换
                session_id=self.id,       # 主题摘要按会话 id 落到工作区 .memory/topics/
            )
            self.agent.ui = WebUI(self)
            self._inject_history()          # 从磁盘恢复历史消息
            # 构造完成后再盖章：_inject_history 可能从磁盘补回 model_id/reasoning，
            # 之后 _agent_stale() 靠这两个章判断内存 Agent 是否需要重建
            self._agent_model_id = self.model_id
            self._agent_reasoning = self.reasoning
        return self.agent

    def _agent_stale(self) -> bool:
        """内存 Agent 是否已与会话的模型配置脱节（构造之后 model_id/reasoning 被更新过）。
        会话中换模型：字段先更新、Agent 到下一次对话时才重建（历史落盘不丢）。
        """
        return self.agent is not None and (
            (self.model_id, self.reasoning)
            != (getattr(self, "_agent_model_id", None), getattr(self, "_agent_reasoning", None))
        )

    def _inject_history(self) -> None:
        """首次创建 Agent 时，把磁盘上的 user/assistant 消息灌入。"""
        if self._loaded or self.agent is None:
            return
        data = _load_session_file(self.id)
        if data:
            self.title = data.get("title") or self.title
            self.title_custom = bool(data.get("title_custom", False))
            self.workspace_id = data.get("workspace_id") or self.workspace_id
            self.model_id = data.get("model_id") or None
            self.reasoning = model_config.normalize_reasoning(data.get("reasoning"))
            self.web = bool(data.get("web", False))
            self.created_at = data.get("created_at") or self.created_at
            self.updated_at = data.get("updated_at") or self.updated_at
            for m in data.get("messages", []):
                role, content = m.get("role"), m.get("content")
                # 老会话文件里混着内部注入消息（那时还没打标记），一并跳过，
                # 否则它们会被当成用户说过的话灌回模型上下文
                if _is_injected(m):
                    continue
                if role in ("user", "assistant") and isinstance(content, str) and content:
                    msg: dict[str, Any] = {"role": role, "content": content}
                    # 每轮的用量/耗时一并灌回内存（_ 前缀字段不会发给模型接口）
                    if role == "assistant" and (m.get("usage") or m.get("sec") is not None):
                        msg["_stats"] = {"usage": m.get("usage"), "sec": m.get("sec")}
                    # 附件元数据灌回内存，save() 时再原样落盘
                    if role == "user" and m.get("attachments"):
                        msg["_attachments"] = m["attachments"]
                    self.agent.messages.append(msg)
        self._loaded = True

    def save(self) -> None:
        """把当前会话（标题 + user/assistant 消息）写回磁盘。"""
        if self.agent is None:
            return
        messages = []
        for m in self.agent.messages[1:]:   # [0] 是 system
            if _is_injected(m):
                continue    # 内部注入消息不落盘：既不是用户说的话，图片也无法还原
            role, content = m.get("role"), m.get("content")
            # 多模态用户消息（content 数组）：只把文本分片落盘，
            # 附件元数据单独保存（data_url 体积大且文件已在工作区，无需重复存储）
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user" and isinstance(content, str) and content:
                item = {"role": "user", "content": content}
                if m.get("_attachments"):
                    item["attachments"] = m["_attachments"]
                messages.append(item)
            elif role == "assistant" and isinstance(content, str) and content:
                item = {"role": "assistant", "content": content}
                # 该轮的用量/耗时（_run_worker 挂在消息上的 _stats）一并落盘
                stats = m.get("_stats") or {}
                if stats.get("usage"):
                    item["usage"] = stats["usage"]
                if stats.get("sec") is not None:
                    item["sec"] = stats["sec"]
                messages.append(item)
        # 标题 = 首条真实用户消息（剥口语前缀后截断）；用户手动改过名则保留。
        # 模型生成的标题走 session_title()，同时把 title_custom 置真把这行短路掉。
        if messages and not self.title_custom:
            self.title = _derive_title(messages) or self.title
        self.updated_at = time.time()
        data = {
            "id": self.id,
            "title": self.title,
            "title_custom": self.title_custom,
            "workspace_id": self.workspace_id,
            "model_id": self.model_id,        # 本会话的模型配置（None = 跟全局默认）
            "reasoning": self.reasoning,     # 本会话的推理等级（None = 跟全局默认）
            "web": self.web,                 # 本会话的联网搜索开关
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
            pending[2]["approve"] = approve
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

    def answer_ask(self, cid: str, answer: str) -> bool:
        """浏览器对 ask_request 的应答。返回是否真的匹配到了待答项。"""
        with self.confirm_lock:
            pending = self.pending_ask
            if not pending or pending[0] != cid:
                return False
            pending[2]["answer"] = answer
            pending[1].set()
        return True

    def deny_pending_ask(self) -> None:
        """以「未作答」解除正在等待的提问（停止任务 / 浏览器断连时调用）。

        与 deny_pending_confirm 同理：ask_user 挂在工作线程的 Event.wait 上，
        取消令牌叫不醒它；不主动放行的话 worker 会一直卡到 120s 超时。
        """
        with self.confirm_lock:
            pending = self.pending_ask
            if pending:
                pending[2]["answer"] = ""
                pending[1].set()


class WebUI:
    """Agent 的 UI 实现：10 个回调全部翻译成事件，推入当前会话队列。

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

    def session_title(self, title: str) -> None:
        """模型产出的会话标题（搭记忆提炼那次调用的顺风车，零额外开销）。

        只认「还没被命名」的会话：用户手动改过名（title_custom）就丢弃。
        落地后把 title_custom 置真 —— 标题就此锁定，后续轮次的提炼与规则截断
        都不再改它，免得侧栏标签随对话一直漂。落盘失败不影响任何主流程。
        """
        s = self.session
        clean = _tidy_title(title)
        if not clean or s.title_custom:
            return
        s.title = clean
        s.title_custom = True
        try:
            s.save()
        except Exception:  # noqa: BLE001 —— 标题落盘失败是纯增益丢失，不该上抛
            _log.warning("session.title.save_failed session=%s", s.id, exc_info=True)

    def turn_start(self, turn: int) -> None:
        self._push({"type": "turn", "turn": turn, "mode": self.session.mode})

    def llm_delta(self, _turn: int, text: str, kind: str = "content") -> bool:
        if not text:
            return False
        self._push({"type": "delta", "text": text, "kind": kind})
        # 返回 True 仅限正文：agent 靠它判断「回答已经上屏过」，网页上确实上屏了
        return kind == "content"

    def llm_done(self, _turn: int, latency: float, streamed: bool = False) -> None:
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
                    "args": _brief_args(args, CONFIRM_ARG_LIMIT)})
        got = event.wait(CONFIRM_TIMEOUT)
        with session.confirm_lock:
            session.pending_confirm = None
        return got and holder["approve"]

    def ask_approval(self, reason: str) -> bool:
        """auto 模式 request_approval 的授权桥：复用同一套确认事件/应答协议。

        前端按 tool == "request_approval" 展示「模型主动申请授权」样式，
        申请理由在 args.reason 里。
        """
        session = self.session
        cid = uuid.uuid4().hex[:12]
        event = threading.Event()
        holder = {"approve": False}
        with session.confirm_lock:
            session.pending_confirm = (cid, event, holder)
        self._push({"type": "confirm_request", "id": cid, "tool": "request_approval",
                    "args": {"reason": reason}})
        got = event.wait(CONFIRM_TIMEOUT)
        with session.confirm_lock:
            session.pending_confirm = None
        return got and holder["approve"]

    def ask_user(self, question: str, options: list, allow_free_text: bool = True,
                 multi: bool = False) -> str:
        """ask_user 内联提问桥：推 ask_request 事件 → Event.wait 挂起 → /api/answer 置位。

        与 ask_confirm 共用 confirm_lock 与 CONFIRM_TIMEOUT，但载荷是文本答案。
        前端把它渲染在助手气泡内部，用户点选/输入后即时回传。
        multi=True 时前端渲染多选控件，多选结果仍以「、」拼接的单个字符串回传。
        """
        session = self.session
        cid = uuid.uuid4().hex[:12]
        event = threading.Event()
        holder = {"answer": ""}
        with session.confirm_lock:
            session.pending_ask = (cid, event, holder)
        self._push({"type": "ask_request", "id": cid, "question": question,
                    "options": list(options or []), "allow_free_text": allow_free_text,
                    "multi": multi})
        got = event.wait(CONFIRM_TIMEOUT)
        with session.confirm_lock:
            session.pending_ask = None
        return holder["answer"] if got else ""

    def cancelled(self, reason: str = "") -> bool:
        self._push({"type": "cancelled", "reason": reason or "任务被取消"})
        return True

    def flush_stream(self) -> None:
        pass    # 网页没有「行」的概念，无需收尾


def _brief_args(args: dict, limit: int = 1200) -> dict:
    """参数过墙：每值截断，防止超大参数把事件流撑爆。

    注意：不用 core.ui.brief —— 它会把 \\n 换成 ⏎ 给控制台单行展示用，
    但 SSE 路径需要保留真实换行，让前端代码块正常渲染。
    """
    def _clip(v: object) -> object:
        if isinstance(v, str):
            return v if len(v) <= limit else v[: limit - 1] + "…"
        return v  # dict/list 原样透传，让前端按 JSON 结构自行展示
    return {k: _clip(v) for k, v in (args or {}).items()}


def _session_registry(mode: str, approval: str = "ask", web: bool = False):
    """按行为矩阵克隆一份会话工具表（见方案 §3.1）。

    chat 模式 = 不做推理思考，只保留联网能力（web_search / fetch_url），其余工具不加载；
    web_search 仅在联网开关打开时加载；fetch_url 始终可用（云端 or 本地按本轮模式分派）。
    Tool 对象本身无状态，跨注册表共享引用是安全的。
    request_approval 仅在 auto 档暴露 —— 其它档位模型看不到它。
    """
    from core.registry import REGISTRY, ToolRegistry

    # 记忆总开关关闭时，remember 工具也不进会话工具表（模型看不到就不会调）
    try:
        from core import settings
        memory_on = bool(settings.get("memory_enabled"))
    except Exception:  # noqa: BLE001
        memory_on = True

    reg = ToolRegistry()
    for t in REGISTRY.all():
        if t.name == "web_search":
            if not web:
                continue          # 关闭联网：不提供搜索（只保留本地抓取）
        elif t.name == "fetch_url":
            pass                  # 抓取始终可用：云端 / 本地实现按本轮联网模式分派
        elif mode == "chat":
            continue              # 快速对话：除联网能力外不调用任何工具
        if t.name == "request_approval" and approval != "auto":
            continue
        if t.name == "remember" and not memory_on:
            continue
        reg.add(t)
    return reg


def _remember_from_message(message: str) -> dict | None:
    """处理用户显式「请帮我记住 XXX」：不依赖模型/工具，直接写用户画像。

    两种模式都生效（快速对话没有工具也能记）。返回要推给前端的 memory 事件；
    没有指令时返回 None。任何失败都只记日志，绝不影响本轮对话。
    """
    try:
        from core import memory

        note = memory.parse_remember_command(message)
        if not note:
            return None
        if not memory.is_enabled():
            return {
                "type": "memory", "ok": False, "note": note,
                "error": "记忆功能已关闭（系统配置 → 记忆 里可打开）",
            }
        written = memory.append_user_note(
            note, source="用户指令", kind=memory.infer_user_kind(note)
        )
        _log.info("memory.command session-note chars=%d duplicate=%s", len(note), not written)
        return {"type": "memory", "ok": True, "note": note, "duplicate": not written}
    except Exception:  # noqa: BLE001
        _log.warning("memory.command.failed", exc_info=True)
        return {"type": "memory", "ok": False, "note": "", "error": "写入记忆失败"}


def _search_backend() -> str:
    """探测当前搜索后端，供 /api/state 报告（工具未加载时返回 unknown）。"""
    try:
        import plugins.web_search as ws

        return ws._backend()
    except Exception:  # noqa: BLE001
        return "unknown"


#: 只写不读的敏感配置：GET 时脱敏回显，PUT 时传空串表示「保持原值」
_SECRET_SETTINGS = ("deepseek_search_api_key",)


def _mask_secret(raw: str) -> str:
    return "****" + raw[-4:] if len(raw) > 4 else "****"


def _settings_public() -> dict:
    """配置回给前端的形态：敏感项清空原值，另用 secrets 给出脱敏提示。"""
    values = settings.get_all()
    secrets: dict[str, str] = {}
    for key in _SECRET_SETTINGS:
        raw = str(values.get(key) or "")
        values[key] = ""
        if raw:
            secrets[key] = _mask_secret(raw)
    return {"values": values, "defaults": settings.defaults(), "secrets": secrets}


# ---------------------------------------------------------- 附件

def _resolve_attachments(attachments: list[dict], ws_root: str) -> list[dict]:
    """把上传接口登记的附件元数据解析成 agent.run() 需要的完整结构。

    按类型各归其位：
      · 图片 → 读文件编成 data_url（受 MAX_UPLOAD_BYTES 限制，超限则退化为路径注记）；
      · 文本 → 读前 MAX_ATTACHMENT_TEXT 字符作为 excerpt 注入消息；
      · 其它 → 只留路径，模型可用工具自行读取。
    文件不存在/读不出时不报错，降级为路径注记 —— 模型至少知道「有个文件在哪」。
    """
    import base64

    out: list[dict] = []
    for att in attachments:
        name = str(att.get("name") or "附件")
        rel = str(att.get("path") or "")
        mime = str(att.get("mime") or "")
        size = int(att.get("size") or 0)
        item: dict = {"name": name, "path": rel, "mime": mime, "size": size}
        # 只允许读工作区 .attachments/ 下的文件，防目录穿越
        fp = os.path.normpath(os.path.join(ws_root, rel))
        if not rel.startswith(ATTACH_DIR + os.sep) or not fp.startswith(os.path.normpath(ws_root)):
            out.append(item)
            continue
        if not os.path.isfile(fp):
            out.append(item)
            continue
        try:
            if mime.startswith("image/") and size <= MAX_UPLOAD_BYTES:
                with open(fp, "rb") as f:
                    data = f.read()
                item["data_url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            elif mime.startswith("text/") or mime == "application/json":
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    item["excerpt"] = f.read(MAX_ATTACHMENT_TEXT)
        except OSError:
            pass
        out.append(item)
    return out


# ---------------------------------------------------------- 工作台文件浏览

def _workspace_path(workspace_id: str, rel: str) -> tuple[str, str]:
    """把工作区内的相对路径解析成绝对路径，并做越界/保护区校验。"""
    from core import memory

    root = os.path.realpath(
        workspace_config.resolve_path(workspace_id or workspace_config.DEFAULT_ID)
    )
    raw = (rel or "").replace("\\", "/").strip()
    if raw in ("", "."):
        ap = root
    else:
        if raw.startswith("/") or os.path.isabs(raw):
            raise ValueError("只支持工作区内的相对路径")
        ap = os.path.realpath(os.path.join(root, raw))
    if ap != root and not ap.startswith(root + os.sep):
        raise PermissionError("路径越界：只能访问当前工作区内的文件")
    if memory.is_memory_path(ap, root):
        raise PermissionError(".memory 是系统记忆目录，禁止访问")
    return root, ap


def _content_disposition(name: str) -> str:
    """下载响应头：ASCII 兜底名 + RFC 5987 的 UTF-8 真名（中文名不乱码）。"""
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


def _int_param(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    """取整型 query 参数并夹到 [lo, hi]，脏值一律退回默认值。"""
    try:
        value = int((params.get(key) or [default])[0])
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _read_table(
    ap: str, row_offset: int, row_limit: int, col_offset: int, col_limit: int
) -> dict:
    """流式解析 CSV/TSV，只保留请求的行×列窗口。

    为什么不用 pandas / openpyxl：标准库 csv 已能正确处理引号、字段内换行与
    转义；这里按行迭代，跳过的行立刻被回收，10M 格的表也不会把内存打满。
    """
    from core.sandbox import decode_bytes

    with open(ap, "rb") as f:
        raw = f.read(_TABLE_MAX_BYTES + 1)
    truncated = len(raw) > _TABLE_MAX_BYTES
    if truncated:
        raw = raw[:_TABLE_MAX_BYTES]
    text, encoding = decode_bytes(raw)

    # .tsv 直接按 Tab 切；.csv 先嗅探一次，识别分号/竖线等变体
    delimiter = "\t"
    if not ap.lower().endswith(".tsv"):
        delimiter = ","
        try:
            sniffed = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
            delimiter = sniffed.delimiter or ","
        except csv.Error:
            pass

    header: list[str] = []
    window: list[list[str]] = []
    total_rows = 0      # 数据行数（不含表头）
    total_cols = 0

    for idx, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if idx == 0:
            header = row[:_TABLE_MAX_COLS]
            if len(row) > _TABLE_MAX_COLS:
                truncated = True
            total_cols = len(header)
            continue
        if total_rows >= _TABLE_MAX_ROWS:
            truncated = True
            break
        total_rows += 1
        cols = len(row)
        if cols > _TABLE_MAX_COLS:
            cols = _TABLE_MAX_COLS
            truncated = True
        if cols > total_cols:
            total_cols = cols
        # 数据行从 1 开始编号，窗口左闭右开
        if row_offset < total_rows <= row_offset + row_limit:
            window.append(row[col_offset:col_offset + col_limit])

    width = max(0, min(col_limit, total_cols - col_offset))
    columns: list[str] = []
    used: dict[str, int] = {}
    for i in range(col_offset, col_offset + width):
        name = (header[i].strip() if i < len(header) else "") or f"列{i + 1}"
        if name in used:            # 重名列加后缀，避免前端 key 冲突
            used[name] += 1
            name = f"{name} ({used[name]})"
        else:
            used[name] = 1
        columns.append(name)

    rows = [r[:width] + [""] * (width - len(r)) for r in window]
    return {
        "columns": columns,
        "rows": rows,
        "row_offset": row_offset,
        "row_limit": row_limit,
        "col_offset": col_offset,
        "col_limit": col_limit,
        "total_rows": total_rows,
        "total_cols": total_cols,
        "encoding": encoding,
        "delimiter": delimiter,
        "truncated": truncated,
    }


def _posix_rel(path: str, root: str) -> str:
    return "" if path == root else os.path.relpath(path, root).replace(os.sep, "/")


# ---------------------------------------------------------- 会话持久化

def _session_file(sid: str) -> str:
    return os.path.join(_SESSIONS_DIR, f"{sid}.json")


def _session_events_file(sid: str) -> str:
    """会话事件流文件（P0 事实层）。

    路径的唯一真相在 `core.events.events_path`（写、读、删三处必须永远一致，
    否则删除会清掉一个不存在的文件）。这里只做一层语义化的别名。
    """
    return events.events_path(sid)


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
            "model_id": data.get("model_id") or "",
            "reasoning": model_config.normalize_reasoning(data.get("reasoning")) or "",
            "web": bool(data.get("web", False)),
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


def _user_text(content: object) -> str:
    """多模态 user 消息（content 数组）只取文本分片；普通字符串原样。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _is_injected(m: dict) -> bool:
    """这条消息是主循环的内部注入，而不是用户真的说了什么。

    目前有两种：工具递图片时附的说明（images.ATTACH_NOTE），以及轮数用尽时
    的收尾提示。它们都是「借 user 角色进对话」，必须排除在落盘、历史回放与
    标题之外 —— 否则一次回复会被它们打断成多个气泡，会话标题也会被顶掉。

    新会话带 _injected 私有标记；老会话文件里只留下了那句固定文案，
    没有标记，所以再用文案前缀兜底，让历史会话同样不再裂开。
    """
    if m.get("_injected"):
        return True
    return _user_text(m.get("content")).startswith(ATTACH_NOTE)


# ---------------------------------------------------------------- 会话标题

_TITLE_MAX = 20
# 标题两端要去掉的装饰：空白、引号、书名号、括号、序号标记、句末标点
_TITLE_TRIM = " \t\u3000\"'“”‘’《》「」『』<>()（）[]【】{}#*·-—,，.。!！?？;；:：、\n\r"
# 切标题时认定的「停顿」：只收全角标点。ASCII 的 . , : 在文件名/URL/版本号里
# 到处都是（sales.csv、http://…），拿它们切会把标题切碎。
_TITLE_BREAK = re.compile(r"[，。！？；、]")
# 首条消息开头常见的口语前缀/敬语：剥掉它们，标题才看得出「到底要做什么」。
# 只收多字短语 —— 光秃秃的「请」「我想」不收（「请假流程」会被误伤）；
# 长短语优先，免得「麻烦你帮我」被「麻烦」抢先切成「你帮我…」。
_TITLE_PREFIXES = tuple(sorted((
    "麻烦你帮我看看", "麻烦你帮我", "麻烦帮我", "请你帮我", "可以帮我", "请帮忙",
    "请你帮忙", "我想请你", "麻烦你", "帮我看一下", "帮我看看", "帮我", "麻烦",
    "请你", "请帮我", "请问一下", "请问", "我想问一下", "想问一下", "问一下",
    "能不能帮我", "能否帮我", "能不能", "能否", "可否", "你好", "您好",
    "看一下", "看下", "看看",
), key=len, reverse=True))


def _tidy_title(text: str, limit: int = _TITLE_MAX) -> str:
    """把一个候选标题规整成能看的短标签：压平空白、去装饰、超长时尽量切在停顿处。

    规则标题与模型标题共用它 —— 模型偶尔会带引号、书名号或「1. 」这种序号。
    """
    s = re.sub(r"\s+", " ", text or "").strip().strip(_TITLE_TRIM)
    s = re.sub(r"^\d+\s*[.、)）]\s*", "", s)
    if not s:
        return ""
    if len(s) <= limit:
        return s.strip(_TITLE_TRIM)
    head = _TITLE_BREAK.split(s, maxsplit=1)[0].strip(_TITLE_TRIM)
    if 2 <= len(head) <= limit:
        return head
    cut = s[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]           # 英文：别切在单词中间
    return (cut or s[:limit]).strip(_TITLE_TRIM)


def _derive_title(messages: list[dict]) -> str:
    """从历史消息里提炼标题（本地规则，不花 token）。

    取第一条真实用户消息（跳过内部注入）剥掉口语前缀后截断。它是两层兜底：
    模型标题还没生成时先顶上，记忆提炼被关掉 / 首轮太短没触发时则为最终值。
    """
    for m in messages:
        if m.get("role") != "user" or _is_injected(m):
            continue
        text = _user_text(m.get("content"))
        if not text.strip():
            continue
        # 附件消息（“[附件：a.pdf（路径 …）]”）用文件名当标题，比机器文案可读
        att = re.match(r"^\s*\[附件：([^（(]+)", text)
        if att:
            return _tidy_title(att.group(1))
        s = re.sub(r"\s+", " ", text).strip()
        changed = True
        while changed:                       # 反复剥：「麻烦你帮我看一下」要剥两次
            changed = False
            for p in _TITLE_PREFIXES:
                if s.startswith(p) and len(s) > len(p) + 1:
                    s = s[len(p):].lstrip(_TITLE_TRIM)
                    changed = True
                    break
        return _tidy_title(s)
    return ""


def _display_items(raw: list[dict]) -> list[dict]:
    """原始消息序列 → 前端展示项（历史回放 / 搜索 / keep 截断三处共用，保证索引口径一致）。

    ReAct 一轮里模型可能多次产出 assistant 文本（边解释边调工具，中间夹着
    tool 角色消息）。这些属于「同一次回复」，必须合并为一个展示气泡，否则
    刷新/切换会话后一条回答会裂成多个气泡、挂多个小工具栏。

    分组规则：
      · 每条 user 消息一个展示项；
      · 相邻的 assistant/tool 消息（直到下一条 user）合并为一个 assistant 项，
        各段文本用空行连接；用量/耗时取最后一个带统计的段（worker 挂在轮末回答）。
      · 主循环的内部注入消息（见 _is_injected）不算「下一条 user」——
        它们直接跨过去继续合并，否则 ReAct 中途递图片会把一次回复切成多个气泡。

    每个展示项附带 "_end"：该项在 raw 中的最后下标，供 keep 截断映射回原始序列。
    """
    items: list[dict] = []
    n = len(raw)
    i = 0
    while i < n:
        role = raw[i].get("role")
        if role == "user":
            if _is_injected(raw[i]):
                i += 1
                continue
            text = _user_text(raw[i].get("content"))
            if text:
                item: dict = {"role": "user", "content": text, "_end": i}
                atts = raw[i].get("_attachments") or raw[i].get("attachments")
                if atts:
                    item["attachments"] = atts
                items.append(item)
            i += 1
        elif role in ("assistant", "tool"):
            segs: list[str] = []
            stats_src: dict | None = None
            j = i
            while j < n:
                seg = raw[j]
                if seg.get("role") in ("assistant", "tool"):
                    if seg.get("role") == "assistant":
                        c = seg.get("content")
                        if isinstance(c, str) and c:
                            segs.append(c)
                        if seg.get("_stats") or seg.get("usage") is not None:
                            stats_src = seg
                    j += 1
                elif _is_injected(seg):
                    j += 1          # 内部注入：跨过去，继续合并同一次回复
                else:
                    break
            if segs:
                item = {"role": "assistant", "content": "\n\n".join(segs), "_end": j - 1}
                if stats_src is not None:
                    usage = stats_src.get("usage") or (stats_src.get("_stats") or {}).get("usage")
                    sec = stats_src.get("sec")
                    if sec is None:
                        sec = (stats_src.get("_stats") or {}).get("sec")
                    if usage:
                        item["usage"] = usage
                    if sec is not None:
                        item["sec"] = sec
                items.append(item)
            i = j
        else:
            i += 1
    return items


def _keep_slice(raw: list[dict], keep: int) -> list[dict] | None:
    """按「展示气泡」口径保留前 keep 项，映射回原始消息切片。

    keep 与前端历史展示项一一对应（同一轮的多段 assistant 合并为一项），
    这样重新生成 / 编辑重发时前端传的下标才能准确切到轮边界。
    返回 None 表示 keep 超出当前消息数。
    """
    if keep == 0:
        return []
    disp = _display_items(raw)
    if keep > len(disp):
        return None
    cut = disp[keep - 1]["_end"]
    kept = raw[: cut + 1]
    # 协议补齐：若切点恰好是带 tool_calls 的 assistant，把后续 tool 回复一并带上，
    # 否则 OpenAI 协议校验会拒绝（每个 tool_call 必须有对应 tool 消息）
    if raw[cut].get("role") == "assistant" and raw[cut].get("tool_calls"):
        j = cut + 1
        while j < len(raw) and raw[j].get("role") == "tool":
            kept.append(raw[j])
            j += 1
    return kept


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

def _lan_addresses() -> list[str]:
    """本机非回环 IPv4 地址列表，出口网卡优先（用于打印「远程访问」地址）。

    拿出口 IP 用 UDP connect：不真正发包，只让内核选出默认路由的网卡；
    再用 getaddrinfo 补上其他网卡（多网卡/虚拟网卡环境），去重保序。
    """
    out: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                out.append(ip)
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out


def _core_version() -> str:
    try:
        import core

        return str(getattr(core, "__version__", ""))
    except Exception:  # noqa: BLE001
        return ""


def _host_of(header: str) -> str:
    """从 Host 头取出主机部分：去掉端口，IPv6 去掉方括号。"""
    host = (header or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    if host.count(":") == 1:          # host:port（裸 IPv6 会有多个冒号）
        return host.split(":", 1)[0]
    return host


def _allowed_hosts() -> frozenset[str]:
    """本机允许的 Host 集合。带 60 秒缓存，避免每个请求都去枚举网卡。"""
    global _allowed_hosts_cache
    now = time.time()
    cached_at, cached = _allowed_hosts_cache
    if cached and now - cached_at < _ALLOWED_HOSTS_TTL:
        return cached

    hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    hosts.update(_lan_addresses())
    name = (socket.gethostname() or "").strip().lower()
    if name:
        hosts.add(name)
        hosts.add(f"{name}.local")

    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "")
    for item in raw.replace(";", ",").split(","):
        h = _host_of(item)
        if h:
            hosts.add(h)

    frozen = frozenset(hosts)
    with _allowed_hosts_lock:
        _allowed_hosts_cache = (now, frozen)
    return frozen


class WebServer:
    """HTTP 服务外壳：装配 handler、会话表与静态托管。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        model: str | None = None,
        system_prompt: str = "",
        max_turns: int | None = None,
        login_enabled: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.model = model
        self.system_prompt = system_prompt
        self.max_turns = max_turns      # None = 交给通用配置决定
        self.login_enabled = login_enabled
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
        _log.info("web.listen host=%s port=%d", self.host, self.port)
        # 绑通配地址时，0.0.0.0 不是能点的 URL —— 本机用回环，局域网地址单独列出来
        if self.host in ("0.0.0.0", "::", ""):
            print(f"Web UI 已启动（本机）：http://127.0.0.1:{self.port}")
            lans = _lan_addresses()
            if lans:
                print(f"远程访问：http://{lans[0]}:{self.port}")
                for ip in lans[1:]:
                    print(f"          http://{ip}:{self.port}")
            else:
                print("远程访问：未检测到局域网 IP（检查网络连接）")
        else:
            print(f"Web UI 已启动：http://{self.host}:{self.port}")
        print("生产模式需先构建前端：cd web && npm run build（开发模式见 README）")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n正在关闭 Web UI …")
        finally:
            self._cancel_running()
            self._httpd.server_close()
            print("Web UI 已停止。")

    def _cancel_running(self) -> None:
        """关停时通知仍在跑的任务收手，避免 daemon 线程被硬杀时留下半截子进程。"""
        with self._lock:
            items = list(self.sessions.values())
        for s in items:
            if not s.busy or s.agent is None:
                continue
            try:
                s.agent.interrupt("服务正在关闭")
            except Exception:  # noqa: BLE001 —— 收尾失败不能阻断退出
                pass

    def shutdown(self) -> None:
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    def request_shutdown(self, reason: str = "") -> None:
        """供 /api/shutdown 与信号处理调用：非阻塞地结束 serve_forever。"""
        _log.info("web.shutdown reason=%s", reason or "-")
        self.shutdown()


class _RequestHandler(BaseHTTPRequestHandler):
    """路由：/api/* + 静态 dist。所有 JSON 交互都在这里，SSE 只属于 /api/chat。"""

    web: WebServer = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    server_version = "mini-agent-web/0.1"

    #: 本次请求的 body 是否已从 rfile 读完（keep-alive 下必须保证，见 _drain_body）
    _body_read = False
    #: 本次请求已解析出的登录用户（由 _guard 填充）
    _user: dict | None = None
    #: 需要在响应头里补发的 Set-Cookie（_json 统一出口，避免各处漏发）
    _resp_headers: list[tuple[str, str]] = []

    # ---------------------------------------------------------- 基础设施

    def handle_one_request(self) -> None:
        """每个请求开始时重置 body 标记（连接是复用的，状态不能跨请求）。"""
        self._body_read = False
        self._user = None
        self._resp_headers = []
        super().handle_one_request()

    def log_message(self, format: str, *args) -> None:  # noqa: A002, A003
        # 默认 INFO 级别不落盘（逐请求日志会刷屏）；排查时把 log_level 调到 DEBUG
        _log.debug("http %s %s | %s", getattr(self, "command", "?"),
                   getattr(self, "path", "?"), format % args)

    def _drain_body(self) -> None:
        """消费掉处理函数没读的请求体。

        为什么必须：protocol_version 是 HTTP/1.1（keep-alive），连接会被复用。
        处理函数只用了路径参数、没读 body 时（如 POST /api/models/<id>/activate），
        残留字节会被当成下一个请求的请求行，后续请求直接 400 / 错位 ——
        前端表现就是「点一下按钮，列表突然空了」。
        """
        if self._body_read:
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > MAX_BODY:
            # 超限不读，直接断开连接，避免残留字节污染后续请求
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def _json(self, obj, status: int = 200) -> None:
        self._drain_body()
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._resp_headers:
            self.send_header(key, value)
        self._resp_headers = []
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str) -> None:
        _log.warning("api.fail status=%d route=%s message=%s", status, self.path, message)
        self._json({"error": message}, status)

    def _read_body(self) -> dict | None:
        if self._body_read:
            return None
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return None
        if length > MAX_BODY:
            self.close_connection = True
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

    def _is_remote(self) -> bool:
        """TCP 对端不是回环地址即为远程客户端（手机/局域网其他设备）。"""
        return self.client_address[0] not in _LOCAL_CLIENTS

    # ---------------------------------------------------------- 登录与会话

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value.strip()
        return ""

    def _set_cookie(self, name: str, value: str, max_age: int) -> None:
        self._resp_headers.append(
            ("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")
        )

    def _session_token(self) -> str:
        return self._cookie(_SESSION_COOKIE)

    def _guard(self, route: str) -> bool:
        """统一登录校验。返回 False 表示已经写好响应，调用方应立即 return。

        白名单只有「登录接口」和「非 /api 的静态资源」——
        静态资源本身不含数据，未登录也能拿到登录页；数据都在 /api 下。
        """
        # Host 校验放在最前面：连域名都不对，就没必要继续解析身份（防 DNS rebinding）
        host = _host_of(self.headers.get("Host") or "")
        if not host or host not in _allowed_hosts():
            _log.warning("http.bad_host host=%r path=%s", self.headers.get("Host"), route)
            self._json({"error": "Host 不被允许", "code": "BAD_HOST"}, 403)
            return False

        if not self.web.login_enabled:
            self._user = dict(_LOCAL_USER)
            return True
        # 健康检查必须免登录：桌面壳在用户登录前就要能探活
        if route in ("/api/auth/login", "/api/health") or not route.startswith("/api/"):
            return True
        user = None
        token = self._session_token()
        if token:
            try:
                user = auth.resolve_session(token)
            except Exception:  # noqa: BLE001 —— 数据库异常按未登录处理，绝不 500
                _log.exception("auth.resolve_failed")
                user = None
        if user is None:
            self._json(
                {"error": "未登录或登录已过期", "code": "UNAUTHENTICATED"}, 401
            )
            return False
        if user["must_change_password"] and route not in (
            # 这三个接口必须放行，否则「未改密」会连自己也认不出来：
            #   me      → 前端要知道当前是谁、是否待改密
            #   password→ 唯一的出口
            #   logout  → 允许退出换个账号
            "/api/auth/me",
            "/api/auth/password",
            "/api/auth/logout",
        ):
            self._json(
                {"error": "请先修改初始密码", "code": "MUST_CHANGE_PASSWORD"}, 403
            )
            return False
        self._user = user
        return True

    def _require_superadmin(self) -> bool:
        user = self._user or {}
        if user.get("role") != auth.ROLE_SUPERADMIN:
            self._json({"error": "仅超级管理员可执行该操作", "code": "FORBIDDEN"}, 403)
            return False
        return True

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if not self._guard(route):
            return
        if route == "/api/auth/me":
            self._handle_auth_me()
        elif route == "/api/health":
            self._handle_health()
        elif route == "/api/users":
            self._handle_users_list()
        elif route == "/api/state":
            self._handle_state()
        elif route == "/api/history":
            self._handle_history(parse_qs(parsed.query))
        elif route == "/api/trajectory":
            self._handle_trajectory(parse_qs(parsed.query))
        elif route == "/api/sessions":
            self._handle_sessions_list(parse_qs(parsed.query))
        elif route == "/api/workspaces":
            self._handle_workspaces_list()
        elif route == "/api/models":
            self._handle_models_list()
        elif route == "/api/settings":
            self._handle_settings_get()
        elif route == "/api/memory":
            self._handle_memory_get(parse_qs(parsed.query))
        elif route == "/api/tools":
            self._handle_tools_list()
        elif route == "/api/workspace/dir":
            self._handle_workspace_dir(parse_qs(parsed.query))
        elif route == "/api/workspace/file":
            self._handle_workspace_file(parse_qs(parsed.query))
        elif route == "/api/workspace/raw":
            self._handle_workspace_raw(parse_qs(parsed.query))
        elif route == "/api/workspace/download":
            self._handle_workspace_download(parse_qs(parsed.query))
        elif route == "/api/workspace/table":
            self._handle_workspace_table(parse_qs(parsed.query))
        elif (m := re.match(r"^/api/workspace/asset/([^/]+)/(.+)$", route)):
            self._handle_workspace_asset(m.group(1), unquote(m.group(2)))
        elif route == "/api/attachment":
            self._handle_attachment_serve(parse_qs(parsed.query))
        else:
            self._serve_static(route)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if not self._guard(route):
            return
        if route == "/api/auth/login":
            self._handle_auth_login()
        elif route == "/api/auth/logout":
            self._handle_auth_logout()
        elif route == "/api/auth/password":
            self._handle_auth_password()
        elif route == "/api/users":
            self._handle_user_create()
        elif (m := re.match(r"^/api/users/(\d+)/logout-all$", route)):
            self._handle_user_logout_all(int(m.group(1)))
        elif route == "/api/shutdown":
            self._handle_shutdown()
        elif route == "/api/chat":
            self._handle_chat()
        elif route == "/api/upload":
            self._handle_upload()
        elif route == "/api/confirm":
            self._handle_confirm()
        elif route == "/api/answer":
            self._handle_answer()
        elif route == "/api/stop":
            self._handle_stop()
        elif route == "/api/sessions":
            self._handle_session_create()
        elif (m := re.match(r"^/api/sessions/([^/]+)/truncate$", route)):
            self._handle_session_truncate(m.group(1))
        elif (m := re.match(r"^/api/sessions/([^/]+)/model$", route)):
            self._handle_session_model(m.group(1))
        elif route == "/api/workspaces/pick":
            self._handle_workspace_pick()
        elif route == "/api/workspaces":
            self._handle_workspace_create()
        elif route == "/api/models":
            self._handle_model_create()
        elif route == "/api/models/test":
            self._handle_model_test_raw()
        elif route == "/api/models/reasoning":
            self._handle_model_reasoning()
        else:
            m = re.match(r"^/api/models/([^/]+)/(test|activate)$", route)
            if m:
                self._handle_model_action(m.group(1), m.group(2))
            else:
                self._fail(404, f"未知接口：{route}")

    def do_PUT(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if not self._guard(route):
            return
        if (m := re.match(r"^/api/users/(\d+)$", route)):
            self._handle_user_update(int(m.group(1)))
            return
        if route == "/api/settings":
            self._handle_settings_put()
            return
        if route == "/api/memory":
            self._handle_memory_put()
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
        if not self._guard(route):
            return
        if (m := re.match(r"^/api/users/(\d+)$", route)):
            self._handle_user_delete(int(m.group(1)))
            return
        if route == "/api/memory/topic":
            self._handle_memory_topic_delete()
            return
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

    # ---------------------------------------------------------- 登录与账号

    def _handle_auth_me(self) -> None:
        self._json({"user": self._user, "login_enabled": self.web.login_enabled})

    def _handle_health(self) -> None:
        """免登录探活接口：桌面壳用它判断「后端起来了没有」。"""
        self._json({
            "ready": True,
            "version": _core_version(),
            "pid": os.getpid(),
            "host": self.web.host,
            "port": self.web.port,
            "login_enabled": self.web.login_enabled,
            "data_dir": data_dir(),
            "web_dist": _DIST,
            "uptime": round(time.time() - _STARTED_AT, 3),
        })

    def _handle_shutdown(self) -> None:
        """优雅关闭：仅本机可用（远程/局域网一律拒绝）。"""
        if self._is_remote():
            self._fail(403, "仅允许本机关闭服务")
            return
        self._json({"ok": True})
        # 先把响应发出去，再在后台停服务器（shutdown 会阻塞 serve_forever）
        threading.Timer(0.15, self.web.request_shutdown, args=("api",)).start()

    def _handle_auth_login(self) -> None:
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{username, password}）")
            return
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        try:
            token, user = auth.login(
                username, password,
                ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent", ""),
            )
        except auth.AuthError as e:
            _log.info("auth.login_failed user=%s code=%s", username[:32], e.code)
            self._json({"error": e.message, "code": e.code},
                       429 if e.code == "LOCKED" else 401)
            return
        self._set_cookie(_SESSION_COOKIE, token, auth.SESSION_TTL)
        _log.info("auth.login_ok user=%s ip=%s", user["username"], self._client_ip())
        self._json({"user": user, "login_enabled": True})

    def _handle_auth_logout(self) -> None:
        auth.delete_session(self._session_token())
        self._set_cookie(_SESSION_COOKIE, "", 0)
        self._json({"ok": True})

    def _handle_auth_password(self) -> None:
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{old_password, new_password}）")
            return
        user = self._user or {}
        try:
            auth.change_password(
                int(user.get("id") or 0),
                str(body.get("old_password") or ""),
                str(body.get("new_password") or ""),
                keep_token=self._session_token(),
            )
        except auth.AuthError as e:
            self._fail(400, e.message)
            return
        self._json({"ok": True, "user": auth.get_user(int(user["id"]))})

    def _handle_users_list(self) -> None:
        if not self._require_superadmin():
            return
        self._json({"users": auth.list_users()})

    def _handle_user_create(self) -> None:
        if not self._require_superadmin():
            return
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON")
            return
        try:
            user = auth.create_user(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                display_name=str(body.get("display_name") or ""),
                role=str(body.get("role") or auth.ROLE_USER),
                must_change_password=bool(body.get("must_change_password", True)),
            )
        except auth.AuthError as e:
            self._fail(400, e.message)
            return
        self._json({"user": user}, 201)

    def _handle_user_update(self, uid: int) -> None:
        if not self._require_superadmin():
            return
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON")
            return
        try:
            user = auth.update_user(
                uid,
                actor_id=int((self._user or {}).get("id") or 0),
                display_name=body.get("display_name") if "display_name" in body else None,
                role=str(body["role"]) if "role" in body else None,
                disabled=bool(body["disabled"]) if "disabled" in body else None,
                password=str(body["password"]) if body.get("password") else None,
                # 管理员重置密码后强制下次登录改密（自助改密走 /api/auth/password，不受影响）
                must_change_password=True if body.get("password") else None,
            )
        except auth.AuthError as e:
            self._fail(400, e.message)
            return
        self._json({"user": user})

    def _handle_user_delete(self, uid: int) -> None:
        if not self._require_superadmin():
            return
        try:
            auth.delete_user(uid, actor_id=int((self._user or {}).get("id") or 0))
        except auth.AuthError as e:
            self._fail(400, e.message)
            return
        self._json({"ok": True})

    def _handle_user_logout_all(self, uid: int) -> None:
        if not self._require_superadmin():
            return
        if auth.get_user(uid) is None:
            self._fail(404, "账号不存在")
            return
        auth.delete_user_sessions(uid)
        self._json({"ok": True})

    # ---------------------------------------------------------- 状态

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
            "max_context_chars": settings.get("max_context_chars") or 200000,
            "sessions": sessions,
            "remote": self._is_remote(),
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
        # 同一轮 ReAct 的多个 assistant 文本段合并成一个气泡（_end 是内部字段，不外发）
        messages = [
            {k: v for k, v in it.items() if k != "_end"}
            for it in _display_items(src)
        ]
        self._json({"session": sid, "messages": messages})

    def _handle_trajectory(self, params: dict) -> None:
        """只读：回放会话事件流（P0 事实层），供前端 Trajectory 视图使用。

        无事件文件（P0 之前的老会话）→ 返回空数组，不报 404。
        limit 默认 500、上限 5000；from_seq 只取 seq 大于它的增量。
        """
        sid = str((params.get("session") or ["default"])[0])[:64]

        def _int(name: str, default: int) -> int:
            try:
                return int((params.get(name) or [default])[0])
            except (TypeError, ValueError):
                return default

        from_seq = max(0, _int("from_seq", 0))
        limit = max(1, min(_int("limit", 500), 5000))
        items = events.load_events(sid, from_seq=from_seq, limit=limit)
        self._json({"session": sid, "events": items, "last_seq": events.last_seq(sid)})

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
                # 索引口径必须与历史回放一致（同一轮多段 assistant 已合并），
                # 否则 hit_index 定位会错位
                msgs = [d["content"] for d in _display_items(data.get("messages", []))]
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
        """删除一个会话（磁盘文件 + 内存 + 对应主题摘要）。"""
        sid = sid[:64]
        # 先解析工作区（内存对象优先，否则读磁盘），用于删掉 .memory/topics 下的摘要
        wid = "default"
        sess = self.web.sessions.get(sid)
        if sess is not None:
            wid = sess.workspace_id or "default"
        else:
            data = _load_session_file(sid)
            if data:
                wid = data.get("workspace_id") or "default"
        with self.web._lock:
            self.web.sessions.pop(sid, None)
        ok = _delete_session_file(sid)
        # 事件流文件一并删除（失败不阻断 —— 与主题摘要处理一致）
        try:
            os.remove(_session_events_file(sid))
        except OSError:
            pass
        # 摘要清理失败不阻断会话删除（记忆是衍生物）
        try:
            from core import memory
            memory.delete_topic(sid, workspace_config.resolve_path(wid))
        except Exception:  # noqa: BLE001
            pass
        if ok:
            self._json({"ok": True})
        else:
            self._fail(404, "会话不存在")

    def _handle_session_truncate(self, sid: str) -> None:
        """截断会话：保留前 keep 条 user/assistant 消息（不含 system），删除其后的全部内容。

        前端用它实现「重新生成 / 编辑重发」：截掉末尾不满意的几轮，
        保留的轮次按原样恢复，然后前端用保留的 user 消息重发。
        """
        sid = sid[:64]
        body = self._read_body()
        keep = int(body.get("keep") or 0) if body else 0
        if keep < 0:
            self._fail(400, "keep 必须是非负整数")
            return
        session = self.web.session(sid)
        # 内存优先（含最新态），否则从磁盘恢复后再改
        if session.agent is not None:
            # keep 按前端展示气泡计数：同一轮多段 assistant 合并为一项
            msgs = session.agent.messages[1:]   # [0] 是 system
            kept = _keep_slice(msgs, keep)
            if kept is None:
                self._fail(400, "keep 超出当前消息数量")
                return
            session.agent.messages = session.agent.messages[:1] + kept
            session.save()
        else:
            data = _load_session_file(sid)
            if data is None:
                self._fail(404, "会话不存在")
                return
            kept = _keep_slice(data.get("messages", []), keep)
            if kept is None:
                self._fail(400, "keep 超出当前消息数量")
                return
            data["messages"] = kept
            _save_session_file(sid, data)
        self._json({"ok": True, "keep": keep})

    def _handle_session_model(self, sid: str) -> None:
        """设置本会话的配置：{model_id?, reasoning?, web?}（缺省项保持原值）。
        每个对话单独保存自己的模型与联网选择，互不影响；全局 active 只管新会话的缺省。
        """
        sid = sid[:64]
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{model_id, reasoning}）")
            return
        data = _load_session_file(sid)
        if data is None:
            self._fail(404, "会话不存在")
            return
        if "model_id" in body:
            mid = str(body.get("model_id") or "")[:64]
            if mid and model_config.get_entry(mid) is None:
                self._fail(400, "模型配置不存在")
                return
        else:
            mid = data.get("model_id") or ""
        if "reasoning" in body:
            rlv = str(body.get("reasoning") or "")[:16]
            if rlv and rlv not in model_config.REASONING_LEVELS:
                self._fail(400, "推理档位不合法")
                return
        else:
            rlv = model_config.normalize_reasoning(data.get("reasoning")) or ""
        if "web" in body:
            wv = bool(body.get("web"))
        else:
            wv = bool(data.get("web", False))
        data["model_id"] = mid or None
        data["reasoning"] = rlv or None
        data["web"] = wv
        _save_session_file(sid, data)
        # 内存中的会话同步更新字段；Agent 交给下一次对话的过期检查（_agent_stale）
        # 去重建 —— 正在运行的 worker 仍握着旧 Agent 引用，不受影响
        with self.web._lock:
            sess = self.web.sessions.get(sid)
            if sess is not None:
                sess.model_id = mid or None
                sess.reasoning = rlv or None
                sess.web = wv
        self._json({"ok": True, "model_id": mid or None, "reasoning": rlv or None, "web": wv})

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
            "reasoning": model_config.reasoning_level(),
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

    def _handle_model_reasoning(self) -> None:
        """设置全局推理等级（off/low/high/max）。"""
        body = self._read_body() or {}
        try:
            level = model_config.set_reasoning(str(body.get("level") or ""))
        except ValueError as e:
            self._fail(400, str(e))
            return
        self.web.reset_agents()          # 等级变化 → 已有会话 Agent 重建
        self._json({"ok": True, "reasoning": level})

    # ------------------------------------------------ 工具 / 插件列表 API

    def _handle_tools_list(self) -> None:
        """返回全部可用工具列表（供插件管理界面展示）。"""
        from core.registry import REGISTRY

        tools = []
        for t in REGISTRY.all():
            tools.append({
                "name": t.name,
                "description": t.description,
                "group": t.group,
                "source": t.source,
                "module": t.module,
                "dangerous": t.dangerous,
                "exclusive": t.exclusive,
                "signature": t.signature(),
            })
        groups = {}
        for g, ts in REGISTRY.groups().items():
            groups[g] = [t.name for t in ts]
        self._json({
            "tools": tools,
            "groups": groups,
            "sources": REGISTRY.sources(),
        })

    # ------------------------------------------------ 通用配置 API

    def _handle_settings_get(self) -> None:
        """通用配置：当前有效值 + 内置默认值（前端用来做「恢复默认」）。"""
        self._json(_settings_public())

    def _handle_settings_put(self) -> None:
        """更新通用配置：{...字段} 或 {"reset": true}。

        改完立刻生效：影响进程全局的项同步到运行时，其余靠重建会话 Agent 落地。
        """
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON")
            return
        reset = bool(body.pop("reset", False))
        # 敏感项留空 = 前端没有改动（GET 不回传原值），直接丢弃避免清空
        for key in _SECRET_SETTINGS:
            if key in body and not str(body.get(key) or "").strip():
                body.pop(key)
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
        self._json(_settings_public())

    # ------------------------------------------------------------ 记忆管理

    def _handle_memory_get(self, params: dict) -> None:
        """读取三层记忆：?workspace_id=xxx（缺省=默认工作区）。

        返回用户画像全文、当前项目记忆全文与真实磁盘路径、主题摘要列表（含正文）。
        记忆是用户自己的数据，远程访问同样可读写（多用户鉴权未来统一接入）。
        """
        from core import memory

        wid = (params.get("workspace_id") or ["default"])[0]
        try:
            ws_root = workspace_config.resolve_path(wid)
        except Exception:  # noqa: BLE001
            self._fail(400, "工作区不存在")
            return
        topics = []
        for it in memory.list_topics(ws_root):
            topics.append({
                "session": it["session"],
                "mtime": it["mtime"],
                "mtime_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(it["mtime"])),
                "size": it["size"],
                "content": memory.load_topic(it["session"], ws_root),
            })
        self._json({
            "user_profile": memory.load_user_profile(),
            "user_profile_path": memory.user_profile_path(),
            "project": memory.load_project(ws_root),
            "project_path": memory.project_path(ws_root),
            "workspace_root": ws_root,
            "topics": topics,
        })

    def _handle_memory_put(self) -> None:
        """保存手工编辑的记忆：{scope: user|project, content, workspace_id?}。"""
        from core import memory

        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON")
            return
        scope = str(body.get("scope") or "").strip()
        content = str(body.get("content") or "")
        if scope not in ("user", "project"):
            self._fail(400, "scope 只能是 user / project")
            return
        try:
            if scope == "user":
                memory.save_user_profile(content)
            else:
                wid = str(body.get("workspace_id") or "default")
                ws_root = workspace_config.resolve_path(wid)
                memory.save_project(content, ws_root)
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"保存失败：{e}")
            return
        self._json({"ok": True})

    def _handle_memory_topic_delete(self) -> None:
        """删除一条主题摘要：{session, workspace_id?}。"""
        from core import memory

        body = self._read_body() or {}
        sid = str(body.get("session") or "")[:80]
        if not sid:
            self._fail(400, "缺少 session")
            return
        wid = str(body.get("workspace_id") or "default")
        try:
            ws_root = workspace_config.resolve_path(wid)
            memory.delete_topic(sid, ws_root)
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"删除失败：{e}")
            return
        self._json({"ok": True})

    def _handle_upload(self) -> None:
        """接收 base64 附件，写入工作区 .attachments/，返回元数据。

        前端先上传再发送：/api/chat 只传元数据（path/mime/size），
        工作线程在发送前用 _resolve_attachments 读文件编成 data_url/节选。
        """
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{name, data, mime, workspace}）")
            return
        name = str(body.get("name") or "").strip()
        data_b64 = str(body.get("data") or "")
        mime = str(body.get("mime") or "application/octet-stream")
        ws = str(body.get("workspace") or "").strip()
        if not name or not data_b64:
            self._fail(400, "缺少 name 或 data")
            return

        import base64

        try:
            data = base64.b64decode(data_b64)
        except Exception:
            self._fail(400, "base64 解码失败")
            return
        if len(data) > MAX_UPLOAD_BYTES:
            self._fail(413, f"附件过大：{len(data)} 字节（上限 {MAX_UPLOAD_BYTES}）")
            return

        ws_root = workspace_config.resolve_path(ws or workspace_config.DEFAULT_ID)
        att_dir = os.path.join(ws_root, ATTACH_DIR)
        os.makedirs(att_dir, exist_ok=True)

        # 防重名 + 防目录穿越：UUID 前缀 + 清洗文件名
        safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "unnamed"
        filename = f"{uuid.uuid4().hex[:8]}-{safe}"
        fp = os.path.join(att_dir, filename)
        with open(fp, "wb") as f:
            f.write(data)

        self._json({
            "name": name,
            "path": f"{ATTACH_DIR}/{filename}",
            "mime": mime,
            "size": len(data),
        })

    def _handle_attachment_serve(self, params: dict) -> None:
        """从工作区 .attachments/ 读文件，供前端展示图片/下载。

        防目录穿越：只允许读 .attachments/ 子目录，且最终路径必须落在工作区内。
        """
        rel = (params.get("path") or [""])[0]
        ws = (params.get("workspace") or [""])[0]
        if not rel:
            self._fail(400, "缺少 path 参数")
            return
        ws_root = workspace_config.resolve_path(ws or workspace_config.DEFAULT_ID)
        fp = os.path.normpath(os.path.join(ws_root, rel))
        if not rel.startswith(ATTACH_DIR + os.sep) and not rel.startswith(ATTACH_DIR + "/"):
            self._fail(403, "只能读取 .attachments/ 下的文件")
            return
        if not fp.startswith(os.path.normpath(ws_root)):
            self._fail(403, "路径越界")
            return
        if not os.path.isfile(fp):
            self._fail(404, "文件不存在")
            return

        ext = os.path.splitext(fp)[1].lower()
        mime = _CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            self._fail(500, "读取失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------- 工作台文件浏览

    def _handle_workspace_dir(self, params: dict) -> None:
        wid = (params.get("workspace_id") or [workspace_config.DEFAULT_ID])[0]
        rel = (params.get("path") or [""])[0]
        try:
            root, ap = _workspace_path(wid, rel)
        except ValueError as e:
            self._fail(400, str(e))
            return
        except PermissionError as e:
            self._fail(403, str(e))
            return
        if not os.path.isdir(ap):
            self._fail(404, "目录不存在")
            return

        entries: list[dict] = []
        truncated = False
        try:
            with os.scandir(ap) as it:
                for entry in it:
                    if len(entries) >= _DIR_MAX_ENTRIES:
                        truncated = True
                        break
                    try:
                        is_dir = entry.is_dir()
                        is_file = entry.is_file()
                    except OSError:
                        continue
                    if not is_dir and not is_file:
                        continue
                    if is_dir and entry.name in _HIDDEN_DIRS:
                        continue
                    # 指向工作区外的符号链接不展示，避免点开才发现 403
                    if entry.is_symlink():
                        real = os.path.realpath(entry.path)
                        if real != root and not real.startswith(root + os.sep):
                            continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    entries.append({
                        "name": entry.name,
                        "path": _posix_rel(os.path.realpath(entry.path), root),
                        "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        "ext": "" if is_dir else os.path.splitext(entry.name)[1].lower(),
                    })
        except OSError as e:
            self._fail(500, f"读取目录失败：{e}")
            return

        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        rel_path = _posix_rel(ap, root)
        parent = "/".join(rel_path.split("/")[:-1]) if rel_path else ""
        self._json({
            "workspace_id": wid,
            "root": root,
            "path": rel_path,
            "parent": parent,
            "entries": entries,
            "truncated": truncated,
        })

    def _handle_workspace_file(self, params: dict) -> None:
        wid = (params.get("workspace_id") or [workspace_config.DEFAULT_ID])[0]
        rel = (params.get("path") or [""])[0]
        try:
            root, ap = _workspace_path(wid, rel)
        except ValueError as e:
            self._fail(400, str(e))
            return
        except PermissionError as e:
            self._fail(403, str(e))
            return
        if not os.path.isfile(ap):
            self._fail(404, "文件不存在")
            return

        name = os.path.basename(ap)
        ext = os.path.splitext(name)[1].lower()
        rel_path = _posix_rel(ap, root)
        try:
            st = os.stat(ap)
        except OSError as e:
            self._fail(500, f"读取文件信息失败：{e}")
            return
        base = {
            "name": name,
            "path": rel_path,
            "ext": ext,
            "size": st.st_size,
            "mtime": st.st_mtime,
        }

        kind = _BINARY_PREVIEWS.get(ext)
        if kind:
            if st.st_size > _RAW_MAX_BYTES:
                self._json({**base, "kind": "unsupported",
                            "reason": f"文件超过 {_RAW_MAX_BYTES // (1024 * 1024)}MB，暂不预览"})
                return
            url = (
                f"/api/workspace/raw?workspace_id={quote(wid)}"
                f"&path={quote(rel_path)}"
            )
            self._json({**base, "kind": kind, "url": url})
            return

        if ext in (".mht", ".mhtml"):
            if st.st_size > _RAW_MAX_BYTES:
                self._json({**base, "kind": "unsupported",
                            "reason": f"归档超过 {_RAW_MAX_BYTES // (1024 * 1024)}MB，暂不预览"})
                return
            try:
                with open(ap, "rb") as f:
                    archive = f.read()
                from core.mhtml import parse_mhtml

                html, resources = parse_mhtml(archive, name)
            except Exception as e:  # noqa: BLE001 —— 损坏归档降级为不支持
                self._json({**base, "kind": "unsupported",
                            "reason": f"MHTML 解析失败：{e}"})
                return
            self._json({
                **base,
                "kind": "mhtml",
                "html": html,
                "resources": resources,
            })
            return

        if ext in _TABLE_EXTS:
            if st.st_size > _TABLE_MAX_BYTES:
                self._json({**base, "kind": "unsupported",
                            "reason": f"表格超过 {_TABLE_MAX_BYTES // (1024 * 1024)}MB，暂不预览"})
                return
            page = _read_table(ap, 0, _TABLE_PAGE_ROWS, 0, _TABLE_PAGE_COLS)
            url = (
                f"/api/workspace/table?workspace_id={quote(wid)}"
                f"&path={quote(rel_path)}"
            )
            self._json({**base, "kind": "table", "url": url, "table": page})
            return

        if st.st_size > _RAW_MAX_BYTES:
            self._json({**base, "kind": "unsupported",
                        "reason": f"文件超过 {_RAW_MAX_BYTES // (1024 * 1024)}MB，暂不预览"})
            return

        try:
            with open(ap, "rb") as f:
                data = f.read(_TEXT_PREVIEW_BYTES + 1)
        except OSError as e:
            self._fail(500, f"读取文件失败：{e}")
            return

        truncated = len(data) > _TEXT_PREVIEW_BYTES
        if truncated:
            data = data[:_TEXT_PREVIEW_BYTES]
            # 尽量在换行处截断，避免把多字节字符切成乱码
            cut = data.rfind(b"\n")
            if cut > _TEXT_PREVIEW_BYTES // 2:
                data = data[:cut]

        from core.sandbox import decode_bytes, looks_binary

        if looks_binary(data):
            self._json({**base, "kind": "binary",
                        "reason": "二进制文件，暂不支持预览"})
            return

        text, encoding = decode_bytes(data)
        lines = text.splitlines()
        if len(lines) > _TEXT_PREVIEW_LINES:
            lines = lines[:_TEXT_PREVIEW_LINES]
            truncated = True
        # 超长单行（压缩后的 JS、单行 JSON）会拖死前端高亮，先按行截断
        if any(len(line) > _TEXT_LINE_MAX_CHARS for line in lines):
            lines = [line[:_TEXT_LINE_MAX_CHARS] for line in lines]
            truncated = True
        content = "\n".join(lines)
        # 再压一层总字符上限：5000 行 × 4000 字符仍可能让高亮卡住
        if len(content) > _TEXT_PREVIEW_CHARS:
            cut = content.rfind("\n", 0, _TEXT_PREVIEW_CHARS)
            content = content[:cut if cut > 0 else _TEXT_PREVIEW_CHARS]
            truncated = True
        self._json({
            **base,
            "kind": "text",
            "encoding": encoding,
            "content": content,
            "lines": content.count("\n") + 1 if content else 0,
            "truncated": truncated,
        })

    def _handle_workspace_raw(self, params: dict) -> None:
        wid = (params.get("workspace_id") or [workspace_config.DEFAULT_ID])[0]
        rel = (params.get("path") or [""])[0]
        try:
            _root, ap = _workspace_path(wid, rel)
        except ValueError as e:
            self._fail(400, str(e))
            return
        except PermissionError as e:
            self._fail(403, str(e))
            return
        ext = os.path.splitext(ap)[1].lower()
        if ext not in _BINARY_PREVIEWS:
            self._fail(403, "该文件不支持原始字节读取")
            return
        if not os.path.isfile(ap):
            self._fail(404, "文件不存在")
            return
        try:
            size = os.path.getsize(ap)
            if size > _RAW_MAX_BYTES:
                self._fail(413, "文件过大，暂不预览")
                return
            with open(ap, "rb") as f:
                data = f.read()
        except OSError:
            self._fail(500, "读取失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _handle_workspace_download(self, params: dict) -> None:
        """下载工作区内的单个文件：任意类型、任意大小，按附件落盘。"""
        wid = (params.get("workspace_id") or [workspace_config.DEFAULT_ID])[0]
        rel = (params.get("path") or [""])[0]
        try:
            _root, ap = _workspace_path(wid, rel)
        except ValueError as e:
            self._fail(400, str(e))
            return
        except PermissionError as e:
            self._fail(403, str(e))
            return
        if not os.path.isfile(ap):
            self._fail(404, "文件不存在")
            return
        name = os.path.basename(ap)
        ext = os.path.splitext(name)[1].lower()
        try:
            size = os.path.getsize(ap)
            f = open(ap, "rb")
        except OSError:
            self._fail(500, "读取失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", _content_disposition(name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with f:
                while chunk := f.read(64 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass   # 用户中途取消下载，无需报错

    def _handle_workspace_table(self, params: dict) -> None:
        """CSV/TSV 表格分页：按 row_offset/col_offset 返回一个窗口。"""
        wid = (params.get("workspace_id") or [workspace_config.DEFAULT_ID])[0]
        rel = (params.get("path") or [""])[0]
        try:
            _root, ap = _workspace_path(wid, rel)
        except ValueError as e:
            self._fail(400, str(e))
            return
        except PermissionError as e:
            self._fail(403, str(e))
            return
        if not os.path.isfile(ap):
            self._fail(404, "文件不存在")
            return
        if os.path.splitext(ap)[1].lower() not in _TABLE_EXTS:
            self._fail(400, "只有 CSV / TSV 支持表格预览")
            return
        try:
            size = os.path.getsize(ap)
        except OSError:
            self._fail(500, "读取失败")
            return
        if size > _TABLE_MAX_BYTES:
            self._fail(413, "表格过大，暂不预览")
            return
        page = _read_table(
            ap,
            _int_param(params, "row_offset", 0, 0, _TABLE_MAX_ROWS),
            _int_param(params, "row_limit", _TABLE_PAGE_ROWS, 1, _TABLE_PAGE_ROWS),
            _int_param(params, "col_offset", 0, 0, _TABLE_MAX_COLS),
            _int_param(params, "col_limit", _TABLE_PAGE_COLS, 1, _TABLE_PAGE_COLS),
        )
        self._json(page)

    def _handle_workspace_asset(self, workspace_id: str, rel: str) -> None:
        """HTML 预览的静态资源：按路径读取工作区内任意文件。

        为什么用路径而不是 query：预览会注入 `<base href="/api/workspace/asset/<wid>/<dir>/">`，
        相对 URL（含 CSS 内部的 url(...)）才能被浏览器自然解析。
        """
        try:
            _root, ap = _workspace_path(workspace_id, rel)
        except ValueError as e:
            self._fail(400, str(e))
            return
        except PermissionError as e:
            self._fail(403, str(e))
            return
        if not os.path.isfile(ap):
            self._fail(404, "文件不存在")
            return
        try:
            size = os.path.getsize(ap)
            if size > _RAW_MAX_BYTES:
                self._fail(413, "文件过大，暂不加载")
                return
            with open(ap, "rb") as f:
                data = f.read()
        except OSError:
            self._fail(500, "读取失败")
            return
        ext = os.path.splitext(ap)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _handle_chat(self) -> None:
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{message, mode, web}）")
            return
        message = str(body.get("message") or "").strip()
        # 附件列表：[{name, path, mime, size}]，path 相对工作区（.attachments/ 下）
        raw_atts = body.get("attachments")
        attachments = [a for a in (raw_atts or []) if isinstance(a, dict)]
        if not message and not attachments:
            self._fail(400, "message 不能为空")
            return
        mode = body.get("mode") if body.get("mode") in ("chat", "agent") else "agent"
        # 联网开关：前端总是随请求带上本会话的值；未带则沿用会话已保存的配置
        web = bool(body.get("web")) if "web" in body else None
        approval = str(body.get("approval") or "ask")
        if approval not in ("ask", "auto", "full"):
            approval = "ask"
        sid = str(body.get("session") or "default")[:64]

        session = self.web.session(sid)
        if web is None:
            web = session.web
        if not session.run_lock.acquire(blocking=False):
            self._fail(409, "该会话已有任务在执行，请先停止或等待完成")
            return

        # 全新会话（磁盘无记录，含前端「草稿占位」的首条消息）：接受请求带来的
        # 会话级模型配置并落进 session；已开始的会话由 /api/sessions/<id>/model
        # 端点负责变更（与 workspace 同一套「空会话才可指定」的口径）
        if _load_session_file(sid) is None:
            mid = str(body.get("model_id") or "")[:64]
            if mid and model_config.get_entry(mid):
                session.model_id = mid
            rlv = str(body.get("reasoning") or "")[:16]
            if rlv in model_config.REASONING_LEVELS:
                session.reasoning = rlv

        agent = session.ensure_agent()
        # 会话级模型配置与内存 Agent 脱节（磁盘恢复晚于构造 / 上次运行中换过模型）→ 重建
        if session._agent_stale():
            session.agent = None
            session._loaded = False
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
        session.approval_mode = approval
        session.busy = True
        q: queue.Queue = queue.Queue()
        session.queue = q
        # 用户显式「请帮我记住 XXX」：在 run 之前写入用户画像，
        # 这样本轮重建 system prompt 时记忆块里就已经包含它
        mem_event = _remember_from_message(message)
        if mem_event:
            q.put(mem_event)

        worker = threading.Thread(
            target=self._run_worker,
            args=(session, agent, message, mode, q, attachments, approval, web),
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
                    session.deny_pending_ask()
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
        session: _Session, agent, message: str, mode: str, q: queue.Queue,
        attachments: list[dict] | None = None, approval: str = "ask",
        web: bool = False,
    ) -> None:
        """工作线程：换工具表 → agent.run() → 终态事件。

        本轮用量 = run 前后的 usage 差值（agent.usage 是整会话累计的，
        前端要的是「这一条回答花了多少」，所以必须取增量）。
        """
        from core.cancel import Cancelled
        from core.sandbox import set_workspace
        from core import net_mode

        t0 = time.time()
        before = dict(agent.usage)
        # 事件流：与会话 JSON「双写」（JSON 仍是 resume/搜索/展示的主口径）。
        # 它是事实层，写失败只吞掉、绝不影响任务；mirror 把事件即时塞进 SSE
        # 队列做实时轨迹（不阻塞，前端始终以 done 后拉取的全量为准）。
        writer = events.EventWriter(
            session.id,
            enabled=bool(settings.get("events_enabled")),
            value_limit=int(settings.get("events_value_limit") or events.DEFAULT_VALUE_LIMIT),
            mirror=lambda ev: q.put({"type": "trace", "event": ev}),
        )
        agent.event_writer = writer
        writer.set_run(f"r-{uuid.uuid4().hex[:6]}")
        _log.info(
            "chat.start session=%s workspace=%s mode=%s approval=%s chars=%d attachments=%d",
            session.id, session.workspace_id, mode, approval, len(message),
            len(attachments or []),
        )
        try:
            # 本轮联网模式：开 = 云端（DeepSeek 搜索/抓取），关 = 本地抓取
            net_mode.set_mode("cloud" if web else "local")
            agent.net_mode = "cloud" if web else "local"
            # 把沙箱根切到本会话所属工作区，之后工具的相对路径都落在这里
            ws_root = workspace_config.resolve_path(session.workspace_id)
            set_workspace(ws_root)
            agent.approval_mode = approval   # 三档确认逐轮生效
            agent.session_id = session.id   # 工作区切换后仍按本会话沉淀摘要
            agent.registry = _session_registry(mode, approval, web)
            agent.chat_mode = mode == "chat"   # 快速对话：显式关闭思考（推理等级只在智能体模式生效）
            answer = agent.run(
                message, attachments=_resolve_attachments(attachments or [], ws_root)
            )
            sec = round(time.time() - t0, 1)
            usage = _usage_delta(before, agent.usage)
            # 用量/耗时挂到刚完成的 assistant 消息上（_ 前缀不会发给模型），
            # save() 落盘后，刷新页面/重启服务从历史回放也能恢复这些脚注
            for m in reversed(agent.messages):
                if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"]:
                    m["_stats"] = {"usage": usage, "sec": sec}
                    break
            # 先落盘再发终态：前端收到 done 就会刷新会话列表，
            # 反过来的话文件还没写完，新会话/新标题会漏掉一拍
            session.save()
            _log.info(
                "chat.done session=%s sec=%.1f llm_calls=%d tool_calls=%d "
                "prompt_tokens=%d completion_tokens=%d answer_chars=%d",
                session.id, sec, usage["llm_calls"], usage["tool_calls"],
                usage["prompt_tokens"], usage["completion_tokens"], len(answer or ""),
            )
            q.put({"type": "done", "answer": answer,
                   "sec": sec,
                   # 标题还没被用户命名 → 记忆提炼可能在几秒后补一个模型标题，
                   # 前端据此在稍后再拉一次会话列表（这个事件发完连接就关了）
                   "title_pending": not session.title_custom,
                   "usage": usage})
        except Cancelled as e:
            # cancel / run_end{status:"cancelled"} 已由 agent.run() 的取消路径发出，
            # 这里不重复记，避免事件流里出现两条取消书签。
            _log.info("chat.cancelled session=%s reason=%s", session.id, e)
            # ui.cancelled 已发过 cancelled 事件（终态，泵会停）；没发过才补发
            if not getattr(agent, "last_cancel_notice_shown", False):
                q.put({"type": "cancelled", "reason": str(e),
                       "usage": _usage_delta(before, agent.usage)})
        except Exception as e:  # noqa: BLE001 —— 任何异常都转成 error 事件
            _log.exception("chat.error session=%s", session.id)
            # Agent 不吞一般异常（按原样上抛），所以 error 事件只能在这里补记
            writer.emit(events.ERROR, source=events.SRC_LOOP, phase=events.PH_RUN_END,
                        kind=type(e).__name__, message=f"{type(e).__name__}: {e}")
            q.put({"type": "error", "message": f"{type(e).__name__}: {e}",
                   "usage": _usage_delta(before, agent.usage)})
        finally:
            net_mode.reset()
            agent.event_writer = None
            writer.close()

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

    def _handle_answer(self) -> None:
        """ask_user 内联提问的应答：{id, answer, session}。"""
        body = self._read_body()
        if body is None:
            self._fail(400, "请求体必须是 JSON（{id, answer}）")
            return
        sid = str(body.get("session") or "default")[:64]
        session = self.web.session(sid)
        cid = str(body.get("id") or "")
        if not cid:
            self._fail(400, "id 不能为空")
            return
        answer = str(body.get("answer") or "")[:2000]
        matched = session.answer_ask(cid, answer)
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
        session.deny_pending_ask()       # 若卡在内联提问卡片上，置空以解除阻塞
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
        # index/manifest/sw 必须每次回源，否则前端发版后用户会卡在旧壳；
        # 其余产物文件名带内容 hash，内容一变文件名就变，可以永久强缓存。
        fresh = ext in (".html", ".json", ".webmanifest") or os.path.basename(path) == "sw.js"
        self.send_header(
            "Cache-Control",
            "no-cache" if fresh else "public, max-age=31536000, immutable",
        )
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


def _install_signal_handlers(server: WebServer) -> None:
    """把 SIGINT / SIGTERM / SIGBREAK 转成优雅关停。

    为什么要接管：默认行为是抛 KeyboardInterrupt 或直接终止进程，
    正在跑的工具子进程、MCP server 会变成孤儿。
    Windows 只允许主线程注册信号，serve() 一定在主线程调用，所以安全。
    """
    def _handler(signum, _frame) -> None:
        print(f"\n收到信号 {signum}，正在关闭 Web UI …")
        server.request_shutdown(f"signal {signum}")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass   # 非主线程 / 平台不支持：保持默认行为


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    model: str | None = None,
    system_prompt: str = "",
    max_turns: int | None = None,
    login_enabled: bool = True,
) -> None:
    """便捷入口：构建服务并阻塞运行（main.py 的 --web 走这里）。"""
    settings.apply_to_runtime()      # 通用配置里的运行时项（工具输出上限、日志）先落地
    if login_enabled:
        auth.init_db()
        if auth.ensure_default_admin():
            print(
                f"已创建默认管理员账号 {auth.DEFAULT_ADMIN} / "
                f"{auth.DEFAULT_ADMIN_PASSWORD}，首次登录后必须修改密码。"
            )
        auth.cleanup_expired()
    else:
        print("警告：已禁用登录（--no-login），任何能访问该端口的人都能操作本机。")

    server = WebServer(
        host=host, port=port, model=model,
        system_prompt=system_prompt, max_turns=max_turns,
        login_enabled=login_enabled,
    )
    _install_signal_handlers(server)
    server.serve_forever()
