"""
会话事件流 —— 把「模型看到的一切」写成一条只追加的事实流。

为什么需要它？
    会话 JSON（webui.py 的 <sid>.json）只存 user / assistant 两种**展示态**消息，
    中间过程全部丢失：模型当时看到的 system prompt（含记忆块）、每一轮真正发出
    去的 messages 数组、工具参数与结果、确认决策、裁剪动作，都不留痕。于是
    「这一轮模型到底看到了什么」「回答变差是不是某条记忆导致」这类问题无法回答。
    文件日志（logs/）是「给人排查用的」，不是「模型可见的事实」，无法据此重建请求。

本模块做什么？
    只做两件事：**写**一条 append-only 的 JSONL 事实流，以及**读**它并重放。
    它不认识 Agent（agent.py 只依赖它的 emit/rebuild 两个能力），因此可独立测试。

文件与信封
    路径：web_data/sessions/<sid>.events.jsonl（与会话 JSON 同目录、同前缀）。
    格式：一行一个事件，只追加（open(path, "a")），已写行永不修改。
    信封：{"v", "seq", "ts", "type", "source", "phase", "turn", "run_id", "payload"}

核心不变式 I1（请求可重建）
    任一 llm_request 事件里只存逐条 messages 的 sha256（不存正文），正文各存一次
    在自己的来源事件（system_prompt / user_message / llm_response / tool_result /
    context_inject）。`rebuild_messages(sid, seq)` 仅用事件流自身按 seq 重放，即可
    还原当时发送的 messages，并逐条校验 sha256 —— 不读会话 JSON、不读工作区文件。

图片为什么用哈希占位
    多模态消息里的 data_url 是巨大的 base64，写进事件流会让文件爆炸。所以所有
    image_url 分片的 url 在**哈希后就地替换**为 "#img:<sha256>"（写与重放两侧用
    同一套规则），图片正文本身不落盘，但不影响 I1 的严格性。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Iterator

from ._paths import data_dir

_SESSIONS_DIR = os.path.join(data_dir(), "sessions")

#: 信封版本（便于未来演进）
VERSION = 1
#: 单个字符串值的默认截断上限
DEFAULT_VALUE_LIMIT = 65536
#: 截断标记里保留的头/尾长度
_TRUNC_HEAD = 2048
_TRUNC_TAIL = 2048

#: 图片分片 url 的哈希占位前缀（写与重放两侧共用）
IMG_PREFIX = "#img:"
#: 第一级裁剪把工具结果替换成的固定占位符（必须与 agent._trim_old_tool_results 一致）
TRIMMED_PLACEHOLDER = "…[较早的工具结果已省略以节省上下文]…"
#: 滚动摘要消息的前缀（必须与 agent._roll_summary 一致）
SUMMARY_PREFIX = "[历史摘要] "

# ---------------------------------------------------------------- 类型 / 来源 / 阶段

#: 事件类型
RUN_START = "run_start"
RUN_END = "run_end"
SYSTEM_PROMPT = "system_prompt"
USER_MESSAGE = "user_message"
CONTEXT_INJECT = "context_inject"
LLM_REQUEST = "llm_request"
LLM_RESPONSE = "llm_response"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
APPROVAL = "approval"
CANCEL = "cancel"
ERROR = "error"

#: 谁产生了这个事件
SRC_SYSTEM = "system"
SRC_USER = "user"
SRC_MEMORY = "memory"
SRC_MODEL = "model"
SRC_TOOL = "tool"
SRC_APPROVAL = "approval"
SRC_LOOP = "loop"
SRC_BACKFILL = "backfill"

#: 运行阶段（Trajectory 着色与过滤的主维度）
PH_RUN_START = "run_start"
PH_TURN = "turn"
PH_CONTEXT = "context"
PH_RUN_END = "run_end"

#: context_inject 的种类
KIND_MEMORY = "memory"
KIND_TRIM_TOOL_RESULTS = "trim_tool_results"
KIND_SUMMARY = "summary"
KIND_HARD_WINDOW = "hard_window"
KIND_ATTACHMENT_IMAGES = "attachment_images"


# ---------------------------------------------------------------- 路径与哈希

#: 会话 id 白名单：它会被拼进文件名，必须拒绝路径分隔符与 `..`
_SID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def valid_sid(sid: object) -> bool:
    """会话 id 是否合法（Web 层入口校验与此处共用同一规则）。"""
    s = str(sid or "")
    return s not in (".", "..") and ".." not in s and bool(_SID_RE.match(s))


def events_path(sid: str) -> str:
    """会话事件文件路径；非法 id 直接拒绝，绝不拼出目录外的路径。"""
    if not valid_sid(sid):
        raise ValueError(f"非法会话 id：{sid!r}")
    return os.path.join(_SESSIONS_DIR, f"{sid}.events.jsonl")


def events_exist(sid: str) -> bool:
    return os.path.exists(events_path(sid))


def sha256_text(text: str) -> str:
    """文本哈希（utf-8，不转义）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj: Any) -> str:
    """规范化 JSON —— 键排序、紧凑分隔、不转义中文，保证同一对象哈希稳定。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canon_content(content: Any) -> Any:
    """content 规范化：图片分片（image_url）的 url 换成哈希占位，其余原样（含 detail 等兄弟字段）。"""
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            img = part.get("image_url")
            url = img.get("url") if isinstance(img, dict) else None
            if isinstance(url, str):
                # 保留 detail 等兄弟字段：只替换 url，不重建分片
                out.append({**part, "image_url": {**img, "url": _img_marker(url.strip())}})
            else:
                # url 非 str（被截断成 dict / 厂家私有形态）：不参与图片哈希，原样透传
                out.append(part)
        else:
            out.append(part)
    return out


def _img_marker(url: str) -> str:
    """data_url → "#img:<sha256>"；已经是占位符则原样返回（避免二次哈希）。"""
    if url.startswith(IMG_PREFIX):
        return url
    return IMG_PREFIX + sha256_text(url)


def _message_kind(canon: dict) -> str:
    if canon.get("role") == "assistant" and canon.get("tool_calls"):
        return "tool_call"
    if isinstance(canon.get("content"), list):
        return "image"
    return ""


def canonical_content(content: Any) -> Any:
    """content 的规范形态（图片分片换成哈希占位）—— 写事件与重放共用同一规则。"""
    return _canon_content(content)


def content_text(content: Any) -> str:
    """content 的文本表示：字符串原样，多模态取规范化 JSON（用于 chars/sha256）。"""
    c = _canon_content(content)
    return c if isinstance(c, str) else _canon(c)


def canonical_message(m: dict) -> dict:
    """把一条消息压成可重放、可哈希的规范形态：去掉私有字段，图片换成哈希占位。"""
    out: dict[str, Any] = {}
    for k, v in m.items():
        if k.startswith("_"): 
            continue
        out[k] = _canon_content(v) if k == "content" else v
    return out


def message_digest(m: dict) -> dict:
    """一条消息的摘要：{role, sha256, chars, kind?} —— 不存正文（§3.4 的做法）。"""
    canon = canonical_message(m)
    text = _canon(canon)
    d: dict[str, Any] = {
        "role": canon.get("role"),
        "sha256": sha256_text(text),
        "chars": len(text),
    }
    kind = _message_kind(canon)
    if kind:
        d["kind"] = kind
    return d


# ---------------------------------------------------------------- 读取

def iter_events(sid: str) -> Iterator[dict]:
    """逐行读事件；坏行 / 空行跳过（一条坏行不该让整个轨迹不可读）。"""
    path = events_path(sid)
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(ev, dict):
                    yield ev
    except OSError:
        return


def last_seq(sid: str) -> int:
    """文件尾的最大 seq（启动续写用）。文件不存在或为空返回 0。"""
    n = 0
    for ev in iter_events(sid):
        s = ev.get("seq")
        if isinstance(s, int) and s > n:
            n = s
    return n


def load_events(sid: str, from_seq: int = 0, limit: int = 500) -> list[dict]:
    """读一段事件（供 /api/trajectory 用）：seq > from_seq 的前 limit 条。"""
    out: list[dict] = []
    for ev in iter_events(sid):
        s = ev.get("seq")
        if isinstance(s, int) and s > from_seq:
            out.append(ev)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- 截断

def _truncate(text: str, limit: int) -> dict:
    """超长字段 → 摘要。limit 是单字段字符上限：head/tail 合计不超过它，且不超过现状 2048。"""
    half = max(0, int(limit) // 2)
    return {
        "__truncated__": True,
        "len": len(text),
        "sha256": sha256_text(text),
        "head": text[: min(_TRUNC_HEAD, half)],
        "tail": text[-min(_TRUNC_TAIL, half):] if half else "",
    }


#: 事实层正文字段：这些字段参与 I1 重放与 sha256 校验，必须原样保留。
#: 一旦被 _shrink 截断，rebuild_messages 重建出的正文与 llm_request 里的
#: digest 就对不上（重建校验失败），所以按事件类型豁免截断。
#: 代价是事件文件可能明显变大 —— 宁可文件大，也不能让事实流自相矛盾。
_FACT_BODY_FIELDS: dict[str, tuple[str, ...]] = {
    SYSTEM_PROMPT: ("content",),
    USER_MESSAGE: ("content", "attachments"),
    LLM_RESPONSE: ("content", "tool_calls"),
    TOOL_RESULT: ("content",),
    CONTEXT_INJECT: ("content", "parts"),
}


def _shrink_payload(type: str, payload: dict, limit: int) -> dict:
    """按事件类型裁剪 payload：事实层正文豁免，其余（预览/元数据）照旧截断。"""
    keep = _FACT_BODY_FIELDS.get(type, ())
    return {k: (v if k in keep else _shrink(v, limit)) for k, v in payload.items()}


def _shrink(value: Any, limit: int) -> Any:
    """递归把超限的字符串值改写为显式截断标记（不存在静默丢失）。"""
    if isinstance(value, str):
        return value if len(value) <= limit else _truncate(value, limit)
    if isinstance(value, dict):
        return {k: _shrink(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v, limit) for v in value]
    return value


def _is_truncated(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("__truncated__"))


# ---------------------------------------------------------------- 写入

class EventWriter:
    """一次 run 内的事件写入器。agent 挂在实例上，run 结束关闭。

    enabled=False 时是完全的 no-op（不建文件、不分配 seq、不停留任何副作用），
    这是 CLI / repl / 既有测试「零侵入」的保证（决策 D7）。
    """

    def __init__(
        self,
        sid: str,
        *,
        enabled: bool = True,
        value_limit: int = DEFAULT_VALUE_LIMIT,
        mirror=None,
    ) -> None:
        self.sid = sid
        self.enabled = enabled
        self.value_limit = value_limit or DEFAULT_VALUE_LIMIT
        self.mirror = mirror
        self.run_id = ""
        self._seq = last_seq(sid) if self.enabled else 0
        self._fh = None
        if self.enabled:
            try:
                os.makedirs(_SESSIONS_DIR, exist_ok=True)
                self._fh = open(events_path(sid), "a", encoding="utf-8")
            except OSError:
                self.enabled = False
                self._fh = None

    def set_run(self, run_id: str) -> None:
        self.run_id = run_id or ""

    def emit(self, type: str, *, source: str, phase: str = "", turn=None, **payload) -> dict | None:
        """写一条事件；任何失败都吞掉（记录不能影响任务本身）。"""
        if not self.enabled or self._fh is None:
            return None
        try:
            self._seq += 1
            ev = {
                "v": VERSION,
                "seq": self._seq,
                "ts": round(time.time(), 3),
                "type": type,
                "source": source,
                "phase": phase,
                "turn": turn,
                "run_id": self.run_id,
                "payload": _shrink_payload(type, payload, self.value_limit),
            }
            self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:  # noqa: BLE001
            return None
        if self.mirror is not None:
            try:
                self.mirror(ev)
            except Exception:  # noqa: BLE001 —— 实时镜像失败不影响落盘
                pass
        return ev

    def close(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is not None:
            try:
                fh.flush()
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass


# ---------------------------------------------------------------- 重放（I1）

def _user_msg_from_event(p: dict) -> dict | None:
    content = p.get("content")
    if _is_truncated(content):
        content = ""
    msg: dict[str, Any] = {"role": "user", "content": content if content is not None else ""}
    atts = p.get("attachments")
    if atts:
        msg["_attachments"] = atts      # 与 webui 历史回放同一口径（_ 前缀不外发）
    return msg


def _replay(events: list[dict], upto_seq: int) -> tuple[list[dict], str | None]:
    """按 seq 顺序把事件重放成 messages 列表；返回 (messages, 最后一次 system 哈希)。"""
    msgs: list[dict] = []
    sys_sha: str | None = None

    for ev in events:
        seq = ev.get("seq")
        if not isinstance(seq, int) or seq > upto_seq:
            continue
        etype = ev.get("type")
        p = ev.get("payload") or {}
        if not isinstance(p, dict):
            continue

        if etype == SYSTEM_PROMPT:
            content = p.get("content")
            if _is_truncated(content):
                continue          # 正文没记住 → 保持上一条（I1 会因此降级，可接受）
            sys_sha = p.get("sha256")
            msg = {"role": "system", "content": content if content is not None else ""}
            if msgs:
                msgs[0] = msg
            else:
                msgs.append(msg)

        elif etype == USER_MESSAGE:
            m = _user_msg_from_event(p)
            if m is not None:
                msgs.append(m)

        elif etype == LLM_RESPONSE:
            entry: dict[str, Any] = {"role": "assistant", "content": p.get("content") or ""}
            tcs = p.get("tool_calls")
            if tcs:
                entry["tool_calls"] = tcs
            msgs.append(entry)

        elif etype == TOOL_RESULT:
            msgs.append({
                "role": "tool",
                "tool_call_id": p.get("id"),
                "name": p.get("name"),
                "content": p.get("content") or "",
            })

        elif etype == CONTEXT_INJECT:
            kind = p.get("kind")
            if kind == KIND_TRIM_TOOL_RESULTS:
                ids = set(p.get("ids") or [])
                for m in msgs:
                    if m.get("role") == "tool" and m.get("tool_call_id") in ids:
                        m["content"] = TRIMMED_PLACEHOLDER
            elif kind == KIND_SUMMARY:
                prefix_end = int(p.get("prefix_end") or 1)
                boundary = int(p.get("boundary") or 0)
                sm = {"role": "system", "content": SUMMARY_PREFIX + str(p.get("content") or "")}
                msgs = msgs[:prefix_end] + [sm] + msgs[boundary:]
            elif kind == KIND_HARD_WINDOW:
                prefix_end = int(p.get("prefix_end") or 1)
                boundary = int(p.get("boundary") or 0)
                msgs = msgs[:prefix_end] + msgs[boundary:]
            elif kind == KIND_ATTACHMENT_IMAGES:
                parts = p.get("parts")
                if isinstance(parts, list):
                    msgs.append({"role": "user", "content": parts})

    return msgs, sys_sha


def rebuild_messages(sid: str, upto_seq: int | None = None, *, verify: bool = True) -> list[dict]:
    """I1 的实现：仅用事件流重放，还原 upto_seq 时刻发送的 messages。

    upto_seq 指向一条 llm_request 时逐条校验 sha256（不一致即抛 AssertionError，
    表示不变式被破坏）。upto_seq 为 None 时重放到文件尾（供展示兜底用，不做校验）。
    """
    events = list(iter_events(sid))
    if upto_seq is None:
        upto_seq = max(
            (e["seq"] for e in events if isinstance(e.get("seq"), int)), default=0
        )   # 坏 seq（非 int）与 iter_events 同口径：跳过，不参与解析
    target = next((e for e in events if e.get("seq") == upto_seq), None)
    msgs, sys_sha = _replay(events, upto_seq)

    if verify and target is not None and target.get("type") == LLM_REQUEST:
        p = target.get("payload") or {}
        want = str(p.get("system_sha256") or "")
        if want and sys_sha != want:
            raise AssertionError(
                f"I1 破坏：seq={upto_seq} 的 system_sha256 重放不一致（{sys_sha} != {want}）"
            )
        digests = p.get("messages") or []
        if len(digests) != len(msgs):
            raise AssertionError(
                f"I1 破坏：seq={upto_seq} 消息条数不一致（{len(msgs)} != {len(digests)}）"
            )
        for i, (got, m) in enumerate(zip(digests, msgs)):
            if got.get("sha256") != message_digest(m)["sha256"]:
                raise AssertionError(
                    f"I1 破坏：seq={upto_seq} 第 {i} 条消息 sha256 不一致"
                )
    return msgs


def rebuild_display(sid: str) -> list[dict]:
    """展示态兜底：等价于 `_display_items(会话 JSON)`，仅用于 JSON 损坏/缺失时恢复。

    与 webui._display_items 共用同一份合并规则（延迟导入避免循环依赖），因此
    索引口径天然一致。用量/耗时脚注不在此恢复（它们属于展示态，不是事实层）。
    """
    raw = [m for m in rebuild_messages(sid, verify=False) if m.get("role") != "system"]
    from . import webui

    return webui._display_items(raw)
