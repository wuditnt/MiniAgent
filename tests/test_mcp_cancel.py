"""
MCP 取消通知专项测试 —— 验证「取消的另一半」真的走通了。

为什么单独一个文件？
    test_cancel.py 证明的是「本端不再等」；本文件证明的是「对端知道了、并且
    真的停手了」。这两件事是取消的两半，缺任一半都只是半个取消：
      · 只有前一半 → 服务端的慢工具继续跑到底（白占 CPU / 连接 / 配额）；
      · 只有后一半 → 不可能，通知本来就由「不等了」触发。
    所以这里既查客户端发出的通知（协议帧），也查服务端收到后的行为（日志、
    在途表、是否回响应），两头都要有证据。

覆盖 8 件事：
  1  通知真的送达：取消后 server 的 cancel_log 里有对应 requestId 与 reason，
     且服务端提前收手、**不回响应**（客户端一条迟到响应都收不到）
  2  迟到响应被丢弃：对端无视取消照样回响应 → 客户端静默丢弃，不污染后续调用
  3  超时也发取消通知（规范：不再等，就别让对端白跑）
  4  通知幂等：同一 requestId 只发一次
  5  定向取消：取消 A 不影响并发中的 B（按 requestId 精确投递，不是一刀切）
  6  连接关闭时在途请求快速失败（不再干等到自己的超时）
  7  回归：没有取消发生 → 一条通知都不发
  8  全链路：MCPManager + 注册表路径下，取消同样会补发通知

跑法：python test_mcp_cancel.py    （真实 server 子进程，不花 API 费用）
"""

from __future__ import annotations

import json
import os
import threading
import time

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import MCP_SERVER_SCRIPT
from bootstrap import PACKAGE_ROOT as HERE

from core.cancel import CancellationToken, Cancelled, set_current_token  # noqa: E402
from core.mcp import MCPError, MCPStdioClient  # noqa: E402


# ============================================================ 小工具

def new_client(request_timeout: float | None = None) -> MCPStdioClient:
    """建立连接。request_timeout 是**握手之后**单次调用的等待上限。

    为什么不直接写进构造函数？握手要等解释器冷启动，常常超过 1 秒 ——
    把客户端超时压到 1 秒会先让 initialize 失败。要压的是工具调用的等待
    窗口，不是握手，所以握手用默认 20s，握完再收紧。
    """
    c = MCPStdioClient(
        name="demo", command="python", args=[MCP_SERVER_SCRIPT], cwd=HERE, timeout=20.0
    )
    c.start()
    if request_timeout is not None:
        c.timeout = request_timeout
    return c


def cancel_after(token: CancellationToken, seconds: float, reason: str = "") -> threading.Thread:
    def _go():
        time.sleep(seconds)
        token.cancel(reason)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


def wait_for(probe, timeout: float = 3.0, interval: float = 0.05):
    """轮询直到 probe 返回真值 —— 通知的**处理**发生在服务端的另一个线程里，
    主循环不保证「通知」与「紧随其后的查询」的处理顺序，所以要点耐心。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except Exception:  # noqa: BLE001
            last = None
        if last:
            return last
        time.sleep(interval)
    return last


def cancel_log(client: MCPStdioClient) -> dict:
    return json.loads(client.call_tool("cancel_log", {}))


def received_notifications(client: MCPStdioClient, at_least: int = 1) -> list[dict]:
    got = wait_for(
        lambda: (lambda d: d["received"] if len(d["received"]) >= at_least else None)(cancel_log(client))
    )
    return got or []


def server_log(client: MCPStdioClient, keyword: str) -> bool:
    return any(keyword in line for line in client.stderr_tail(200))


def stderr_tail_text(client: MCPStdioClient, n: int = 6) -> str:
    return " / ".join(client.stderr_tail(n))


# ============================================================ 1 通知送达 + 服务端停手

def test_notification_delivered_and_server_stops():
    c = new_client()
    try:
        tok = CancellationToken()
        prev = set_current_token(tok)
        cancel_after(tok, 0.4, "测试：模拟用户中断")
        t0 = time.time()
        try:
            c.call_tool("sleep", {"ms": 8000, "tag": "notify"})
            raise AssertionError("应该抛 Cancelled")
        except Cancelled:
            pass
        elapsed = time.time() - t0
        set_current_token(prev)          # 后面的查询不受已取消令牌影响

        assert elapsed < 3.0, f"本端等待取消太慢：{elapsed:.2f}s"
        assert c.stats["cancellations"] == 1, c.stats

        got = received_notifications(c)
        assert got, f"server 从未收到取消通知（stderr: {stderr_tail_text(c)}）"
        assert "测试：模拟用户中断" in got[0]["reason"], got[0]
        cancelled_rid = got[0]["requestId"]

        # 服务端确实停手了：日志有证据、在途表里已经没有它
        assert wait_for(lambda: server_log(c, "提前收手且不回响应")), stderr_tail_text(c)
        assert wait_for(lambda: server_log(c, "已让在途工具停手")), stderr_tail_text(c)
        assert wait_for(lambda: cancelled_rid not in cancel_log(c)["inflight"]), "server 仍认为它在途"

        # 按规范：被取消的请求不回响应 → 客户端一条迟到响应都不该收到
        time.sleep(0.4)
        assert c.stats["late_responses"] == 0, c.late_responses()
        print(
            f"✓ 通知送达：{elapsed:.2f}s 内本端收手，server 收到 requestId={cancelled_rid}"
            f"（reason 已带过去）并提前停手、不回响应"
        )
    finally:
        c.stop()


# ============================================================ 2 迟到响应被丢弃

def test_late_response_discarded():
    c = new_client()
    try:
        tok = CancellationToken()
        prev = set_current_token(tok)
        cancel_after(tok, 0.3, "测试：放弃这个请求")
        t0 = time.time()
        try:
            c.call_tool("stubborn_sleep", {"ms": 1500, "tag": "stubborn"})
            raise AssertionError("应该抛 Cancelled")
        except Cancelled:
            pass
        cancelled_at = time.time() - t0
        set_current_token(prev)

        assert cancelled_at < 1.0, f"取消太慢：{cancelled_at:.2f}s"
        # 对端 1.5s 后才把响应发出来（它无视了取消通知）
        late = wait_for(lambda: c.stats["late_responses"] >= 1, timeout=4.0)
        assert late, f"迟到响应没有被记录（stderr: {stderr_tail_text(c)}）"
        assert any("stubborn_sleep" in line for line in c.stderr_tail(200))

        # 关键：那条迟到响应不能污染后续调用 —— 同一个连接继续用
        ok = c.call_tool("sha256", {"text": "hello"})
        assert "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in ok, ok
        assert c.stats["late_responses"] == 1, c.late_responses()
        print(
            f"✓ 迟到响应：客户端在 {cancelled_at:.2f}s 放弃，对端 1.5s 后才回的那条被静默丢弃，"
            f"同连接后续调用正常"
        )
    finally:
        c.stop()


# ============================================================ 3 超时也发通知

def test_timeout_sends_cancellation():
    c = new_client(request_timeout=0.8)
    try:
        t0 = time.time()
        try:
            c.call_tool("sleep", {"ms": 3000, "tag": "timeout"})
            raise AssertionError("应该抛 MCPError 超时")
        except MCPError as e:
            assert "超时" in str(e), e
        elapsed = time.time() - t0
        assert elapsed < 2.5, f"超时判定太慢：{elapsed:.2f}s"
        assert c.stats["timeouts"] == 1 and c.stats["cancellations"] == 1, c.stats

        got = received_notifications(c)
        assert got and "超时" in got[0]["reason"], got
        rid = got[0]["requestId"]
        # 服务端拿到超时通知后同样会停手，不会为一个没人要的结果跑满 3 秒
        assert wait_for(lambda: server_log(c, "提前收手且不回响应")), stderr_tail_text(c)
        assert wait_for(lambda: rid not in cancel_log(c)["inflight"]), "server 仍在跑已超时的请求"
        print(f"✓ 超时通知：{elapsed:.2f}s 判定超时并补发通知（reason 含『超时』），对端随即停手")
    finally:
        c.stop()


# ============================================================ 4 幂等

def test_notification_idempotent():
    c = new_client()
    try:
        rid = 987654
        assert c.notify_cancelled(rid, "手工测试") is True
        assert c.notify_cancelled(rid, "手工测试") is False, "同一 requestId 不应重复通知"
        assert c.stats["cancellations"] == 1, c.stats

        got = received_notifications(c)
        same = [r for r in got if r["requestId"] == rid]
        assert len(same) == 1, f"对端收到了重复通知：{got}"
        # 对端对「已经结束的请求」的迟到通知只记录、不处理
        assert wait_for(lambda: server_log(c, "该请求已经结束")), stderr_tail_text(c)
        print("✓ 幂等：同一 requestId 只发一条通知，对端的迟到通知被安全忽略")
    finally:
        c.stop()


# ============================================================ 5 定向取消

def test_cancellation_is_targeted():
    """一个请求超时被取消，同时并发的另一个请求必须毫发无伤。"""
    c = new_client(request_timeout=1.2)
    try:
        out: dict[str, object] = {}

        def slow():
            t0 = time.time()
            try:
                out["slow"] = c.call_tool("sleep", {"ms": 3000, "tag": "A"})
            except MCPError as e:
                out["slow"] = f"MCPError: {e}"
            except Exception as e:  # noqa: BLE001
                out["slow"] = f"{type(e).__name__}: {e}"
            out["slow_elapsed"] = time.time() - t0

        th = threading.Thread(target=slow, daemon=True)
        th.start()
        time.sleep(0.25)                       # 让 A 先进入在途状态
        ok = c.call_tool("sha256", {"text": "hello"})   # 并发中的 B
        assert "2cf24dba" in ok.lower(), ok
        th.join(timeout=8)
        assert "MCPError" in str(out.get("slow")), out

        got = received_notifications(c)
        assert len(got) == 1, f"应当只取消那个超时的请求，实际收到 {len(got)} 条通知：{got}"
        rid = got[0]["requestId"]
        # B 的结果仍然正确返回；被取消的只有 A
        assert wait_for(lambda: rid not in cancel_log(c)["inflight"])
        assert c.stats["cancellations"] == 1, c.stats
        print(
            f"✓ 定向取消：超时的 A（requestId={rid}）被单独取消，"
            f"并发中的 B 正常返回（通知按 requestId 精确投递，不是一刀切）"
        )
    finally:
        c.stop()


# ============================================================ 6 关闭连接时在途请求快速失败

def test_stop_releases_inflight():
    c = new_client()          # 默认 20s：要保证 stop() 先于超时发生
    try:
        out: dict[str, object] = {}

        def slow():
            t0 = time.time()
            try:
                out["result"] = c.call_tool("sleep", {"ms": 6000, "tag": "hang"})
            except Exception as e:  # noqa: BLE001
                out["result"] = f"{type(e).__name__}: {e}"
            out["elapsed"] = time.time() - t0

        th = threading.Thread(target=slow, daemon=True)
        th.start()
        assert wait_for(lambda: len(c.inflight()) == 1, timeout=3), c.inflight()
        time.sleep(0.3)
        t0 = time.time()
        c.stop()
        th.join(timeout=5)
        stop_elapsed = time.time() - t0

        assert not th.is_alive(), "连接关闭后，在途调用没被唤醒"
        assert float(out.get("elapsed", 99)) < 2.5, out
        text = str(out.get("result"))
        assert ("关闭" in text) or ("退出" in text), text
        print(
            f"✓ 关闭连接：在途请求在 {stop_elapsed:.2f}s 内收到明确错误"
            f"（而不是干等 30s 超时）"
        )
    finally:
        c.stop()


# ============================================================ 7 回归

def test_no_cancel_no_notification():
    c = new_client()
    try:
        assert "Python" in c.call_tool("env_info", {})
        assert "tag=x" in c.call_tool("sleep", {"ms": 120, "tag": "x"})
        data = cancel_log(c)
        assert data["received"] == [], f"没有取消却发出了通知：{data}"
        assert c.stats["cancellations"] == 0 and c.stats["timeouts"] == 0, c.stats
        assert c.stats["late_responses"] == 0, c.stats
        print("✓ 回归：无取消时一条通知都不发，协议调用与工具结果一切照旧")
    finally:
        c.stop()


# ============================================================ 8 全链路（注册表 + 管理路径）

def test_registry_path_sends_notification():
    from core.mcp import MCPManager
    from core.registry import ToolRegistry

    reg = ToolRegistry()
    mgr = MCPManager(bootstrap.demo_mcp_config(), HERE, reg)
    mgr.load()
    try:
        assert reg.get("demo__sleep") is not None, "demo server 未挂载"
        tok = CancellationToken()
        prev = set_current_token(tok)
        cancel_after(tok, 0.4, "测试：全链路取消")
        t0 = time.time()
        out = reg.call("demo__sleep", {"ms": 8000, "tag": "e2e"})
        elapsed = time.time() - t0
        set_current_token(prev)

        assert "[cancelled]" in out, out
        assert elapsed < 3.0, f"全链路取消太慢：{elapsed:.2f}s"
        cs = mgr.cancel_stats()
        assert cs["cancellations"] == 1, cs

        client = mgr.clients["demo"]
        got = received_notifications(client)
        assert got and "全链路取消" in got[0]["reason"], got
        # 同一个 server 之后照常可用
        digest = reg.call("demo__sha256", {"text": "hello"})
        assert "2cf24dba" in digest.lower(), digest
        print(f"✓ 全链路：注册表路径下 {elapsed:.2f}s 收场，通知已送达（reason 含本地原因）")
    finally:
        mgr.shutdown()


# ============================================================ main

def main() -> int:
    tests = [
        test_notification_delivered_and_server_stops,
        test_late_response_discarded,
        test_timeout_sends_cancellation,
        test_notification_idempotent,
        test_cancellation_is_targeted,
        test_stop_releases_inflight,
        test_no_cancel_no_notification,
        test_registry_path_sends_notification,
    ]
    print("=== MCP 取消通知专项测试 ===\n")
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
