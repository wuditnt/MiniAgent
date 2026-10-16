"""
一个真实可用的 MCP server（stdio 传输），用于验证 mini-agent 的 MCP 客户端。

它实现了 MCP 协议里最核心的几个方法：
    initialize                握手
    notifications/initialized 握手完成通知（无需响应）
    tools/list                列出工具
    tools/call                调用工具
    ping                      健康检查
    notifications/cancelled   对端放弃某个请求 → 本端停手（无需响应）

协议要点（也是写 MCP server 必须遵守的三条）：
    1. stdout 只能出现 JSON-RPC 消息，一行一条。任何日志都必须写到 stderr，
       否则客户端会把日志当成协议消息解析失败。
    2. 通知（没有 id 的消息）不需要响应；请求必须回同一个 id。
    3. 收到 notifications/cancelled 后 SHOULD 停止处理该请求、且 SHOULD NOT
       再回响应 —— 回一条「已取消」也是错的：客户端已经不认这个 id 了。

关于取消：慢工具必须在循环里主动检查取消事件（_check_cancelled）。
协议只是把「对端不要了」送达到进程，真正停不停手是服务端自己的事 ——
所以「取消通知生效」这条链路必须由服务端配合才成立，本文件就是那个配合方。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import threading
import time

SERVER_INFO = {"name": "mini-agent-demo-mcp", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    """所有日志走 stderr，绝不可以污染 stdout。"""
    print(f"[demo-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 在途请求与取消

class RequestCancelled(Exception):
    """该请求已被对端取消 —— 立刻收手，并且**不要回响应**（协议要求）。"""


_INFLIGHT: dict[int, dict] = {}          # requestId -> {method, event, reason}
_INFLIGHT_LOCK = threading.Lock()
_CANCEL_LOG: list[dict] = []             # 收到的取消通知（供 cancel_log 工具核对）
_TLS = threading.local()                 # 每个请求一个线程 → 用 thread-local 传 rid


def _begin_request(rid, method: str) -> None:
    if rid is None:
        return
    with _INFLIGHT_LOCK:
        _INFLIGHT[rid] = {"method": method, "event": threading.Event(), "reason": ""}


def _end_request(rid) -> None:
    if rid is None:
        return
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(rid, None)


def _mark_cancelled(rid: int, reason: str) -> bool:
    """把某个在途请求标记为已取消。返回 False 表示该请求已经结束。"""
    with _INFLIGHT_LOCK:
        rec = _INFLIGHT.get(rid)
        if rec is None:
            return False
        rec["reason"] = reason
    rec["event"].set()
    return True


def _check_cancelled() -> None:
    """长耗时工具在循环里调用它 —— 这是「取消通知真正生效」的那一行。

    为什么不用「主线程收到通知时直接杀掉工作线程」？
    Python 没有安全的线程强杀，唯一正确做法是协作式：通知到达时置位一个事件，
    工作线程在可以安全收手的位置自己退出（工具不会留下写了一半的状态）。
    """
    rid = getattr(_TLS, "rid", None)
    if rid is None:
        return
    with _INFLIGHT_LOCK:
        rec = _INFLIGHT.get(rid)
    if rec is not None and rec["event"].is_set():
        raise RequestCancelled(
            f"请求 {rid} 已被客户端取消（{rec['reason'] or '未说明原因'}）"
        )


# ---------------------------------------------------------------- 工具实现

def tool_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def tool_sha256(text: str = "", path: str = "") -> str:
    if path:
        with open(path, "rb") as f:
            data = f.read()
        source = f"文件 {path}"
    elif text:
        data = text.encode("utf-8")
        source = "文本"
    else:
        return "[error] 请提供 text 或 path 之一"
    return f"{source} 的 SHA256：{hashlib.sha256(data).hexdigest()}"


def tool_env_info() -> str:
    return (
        f"Python {platform.python_version()} | {platform.system()} {platform.release()} "
        f"| 架构 {platform.machine()} | 解释器 {sys.executable}"
    )


def tool_sleep(ms: int = 500, tag: str = "") -> str:
    """刻意慢的工具 —— 用于验证并发，同时是取消通知的主要验证对象。

    它存在的理由有两个：
      · 测试并发：mini-agent 会并行执行同一轮的多个工具调用，用两个
        sleep(400) 就能量出「4xms 完成还是 1xms 完成」，并靠回显 tag
        证明并发调用之间没有串话。
      · 测试取消：循环里每 50ms 查一次取消事件，客户端一发
        notifications/cancelled，这里就该提前收手（而不是睡满）。
    """
    wait = max(0, min(int(ms), 10_000))
    deadline = time.monotonic() + wait / 1000
    while True:
        _check_cancelled()          # ← 取消通知的落点
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(0.05, left))
    rid = getattr(_TLS, "rid", None)
    return f"tag={tag or '-'} 等待 {wait}ms 完成（服务端 pid={os.getpid()}，req={rid}）"


def tool_stubborn_sleep(ms: int = 1500, tag: str = "") -> str:
    """一个「不听话」的工具：**故意无视取消通知**，睡满后照样回响应。

    存在意义是测试客户端对**迟到响应**的处理：客户端早已放弃这个请求，
    这条响应到达时既不能唤醒任何人，也不能被当成协议消息污染后续调用，
    只能被静默丢弃。现实中的不听话服务端（老版本、第三方实现）很常见，
    客户端必须能扛住。
    """
    wait = max(0, min(int(ms), 10_000))
    time.sleep(wait / 1000)
    log(f"stubborn_sleep 无视了取消通知，仍回响应 tag={tag or '-'}")
    return (
        f"tag={tag or '-'} 无视取消睡了 {wait}ms"
        f"（这条响应到达时，客户端应当已经放弃了这个请求）"
    )


def tool_cancel_log() -> str:
    """回显本 server 收到过的取消通知 —— 让「通知是否真的送达」可被核对。

    这是可观测性，不是业务功能：客户端侧只能证明「我不等了」，
    只有服务端能证明「我知道你不要了」。
    """
    with _INFLIGHT_LOCK:
        received = list(_CANCEL_LOG)
        inflight = sorted(_INFLIGHT)
    return json.dumps(
        {"pid": os.getpid(), "received": received, "inflight": inflight},
        ensure_ascii=False,
    )


TOOLS = [
    {
        "name": "now",
        "description": "返回服务端当前本地时间（格式 YYYY-MM-DD HH:MM:SS）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "impl": tool_now,
    },
    {
        "name": "sha256",
        "description": "计算文本或文件的 SHA256 摘要。text 与 path 二选一。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要计算摘要的文本"},
                "path": {"type": "string", "description": "要计算摘要的文件路径"},
            },
            "required": [],
        },
        "impl": tool_sha256,
    },
    {
        "name": "env_info",
        "description": "返回服务端的运行环境信息（Python 版本、操作系统、解释器路径）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "impl": tool_env_info,
    },
    {
        "name": "sleep",
        "description": "等待指定的毫秒数后返回，用于测试并发调用（会回显 tag）。收到取消通知会提前返回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ms": {"type": "integer", "description": "等待毫秒数", "default": 500},
                "tag": {"type": "string", "description": "回显标记，用于区分并发调用"},
            },
            "required": [],
        },
        "impl": tool_sleep,
    },
    {
        "name": "stubborn_sleep",
        "description": "无视取消通知、睡满指定毫秒后仍返回的工具，专门用于测试客户端的迟到响应处理。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ms": {"type": "integer", "description": "等待毫秒数", "default": 1500},
                "tag": {"type": "string", "description": "回显标记"},
            },
            "required": [],
        },
        "impl": tool_stubborn_sleep,
    },
    {
        "name": "cancel_log",
        "description": "返回本 server 收到过的取消通知与当前在途请求，用于核对取消是否真的送达。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "impl": tool_cancel_log,
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- 协议处理

def send(obj: dict) -> None:
    # 加锁：并发处理请求时，两条 JSON-RPC 消息绝不能在线路上交错，
    # 否则客户端会解析到半行 JSON（这正是「一次一行」协议的硬约束）。
    with _OUT_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


_OUT_LOCK = threading.Lock()


def reply(rid, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def reply_error(rid, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(msg: dict) -> None:
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    is_request = rid is not None

    # 请求先登记在案：nil 之外的一切请求都可能被取消，
    # 而取消通知会在**另一个线程**里到达（主循环一条消息一个线程）。
    if is_request:
        _TLS.rid = rid
        _begin_request(rid, method)
    try:
        _handle(msg, method, rid, params, is_request)
    finally:
        if is_request:
            _TLS.rid = None
            _end_request(rid)


def _handle(msg: dict, method: str, rid, params: dict, is_request: bool) -> None:
    if method == "initialize":
        log(f"客户端初始化：{params.get('clientInfo')}")
        reply(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    elif method == "notifications/initialized":
        log("握手完成")

    elif method == "notifications/cancelled":
        # 规范：params = {requestId, reason?}；这是通知，不回响应。
        target = params.get("requestId")
        reason = params.get("reason") or ""
        if not isinstance(target, int):
            log(f"取消通知格式非法（requestId 必须是整数），已忽略：{params}")
            return
        with _INFLIGHT_LOCK:
            _CANCEL_LOG.append(
                {"requestId": target, "reason": reason, "at": round(time.time(), 3)}
            )
        hit = _mark_cancelled(target, reason)
        log(
            f"收到取消通知 requestId={target} reason={reason!r} → "
            + ("已让在途工具停手" if hit else "该请求已经结束（迟到通知，忽略）")
        )

    elif method == "ping":
        reply(rid, {})

    elif method == "tools/list":
        reply(rid, {
            "tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in TOOLS
            ]
        })

    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOL_MAP.get(name)
        if spec is None:
            reply_error(rid, -32602, f"未知工具：{name}")
            return
        try:
            text = spec["impl"](**args)
            reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        except RequestCancelled:
            # 关键：绝不能被下面的 except Exception 吞掉后回一条响应 ——
            # 协议要求被取消的请求 SHOULD NOT 回响应。
            raise
        except TypeError as e:
            reply(rid, {
                "content": [{"type": "text", "text": f"[error] 参数不匹配：{e}"}],
                "isError": True,
            })
        except Exception as e:  # noqa: BLE001
            reply(rid, {
                "content": [{"type": "text", "text": f"[error] {type(e).__name__}: {e}"}],
                "isError": True,
            })

    elif is_request:
        reply_error(rid, -32601, f"不支持的方法：{method}")


def main() -> int:
    log(f"启动 {SERVER_INFO['name']} v{SERVER_INFO['version']}，等待客户端…")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到非 JSON 输入，已忽略：{line[:80]}")
            continue
        # 每条请求单独一个线程：这样慢工具（如 sleep）不会阻塞后面的请求，
        # 客户端才能真正做到并发调用；取消通知也能在慢工具还睡着的时候被处理。
        # 输出由 _OUT_LOCK 保证不交错。
        threading.Thread(target=_handle_safe, args=(msg,), daemon=True).start()
    log("stdin 已关闭，退出")
    return 0


def _handle_safe(msg: dict) -> None:
    try:
        handle(msg)
    except RequestCancelled as e:
        # 被取消的请求不回响应（客户端已经不认这个 id 了）。
        # 这条日志是「取消通知真的生效了」的证据。
        log(f"请求已取消，提前收手且不回响应：{e}")
    except Exception as e:  # noqa: BLE001
        log(f"处理 {msg.get('method')} 时出错：{type(e).__name__}: {e}")
        if msg.get("id") is not None:
            reply_error(msg["id"], -32603, f"内部错误：{e}")


if __name__ == "__main__":
    raise SystemExit(main())
