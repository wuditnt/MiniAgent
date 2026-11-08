"""Streamable HTTP 传输测试（脚本型）。

自带一个假的 MCP HTTP 服务（标准库 http.server），覆盖：
  1. initialize / Mcp-Session-Id / tools 调用（JSON 响应）
  2. SSE 响应（text/event-stream 里挑出对应 id 的响应）
  3. 超时与取消通知
  4. manager 热加载 + tools/list_changed 自动重载 + notifications/message 日志

不联网、不花钱；只在 `python tests/test_mcp_http.py`（或 run_tests.py）下运行。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import connector_store  # noqa: E402
from core.cancel import CancellationToken, Cancelled, set_current_token  # noqa: E402
from core.mcp import MCPError, MCPHttpClient, MCPManager  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402

if __name__ != "__main__":
    import pytest

    pytest.skip(
        "脚本型用例需自带假服务：python tests/test_mcp_http.py（或 python tests/run_tests.py）",
        allow_module_level=True,
    )

PORT = 8796
BASE = f"http://127.0.0.1:{PORT}/mcp"
SESSION = "sess-http-test"


class FakeMCP(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    notify_once = True
    tools_calls = 0
    seen: list[dict] = []

    def log_message(self, *_args) -> None:  # 静音访问日志
        pass

    # ---------------------------------------------------------- 基础发送

    def _json(self, obj: dict, code: int = 200, headers: dict | None = None) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, messages: list[dict]) -> None:
        raw = b"".join(
            f"event: message\ndata: {json.dumps(m, ensure_ascii=False)}\n\n".encode("utf-8")
            for m in messages
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _empty(self, code: int = 202) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------- 协议

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(size) or b"{}")
        FakeMCP.seen.append(msg)
        method = msg.get("method")
        rid = msg.get("id")

        if method == "initialize":
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "fake-http", "version": "1.0"},
                    },
                },
                headers={"Mcp-Session-Id": SESSION},
            )
            return
        if method in ("notifications/initialized", "notifications/cancelled"):
            self._empty()
            return
        if self.headers.get("Mcp-Session-Id") != SESSION:
            self._json(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "missing session"}},
                code=400,
            )
            return

        if method == "tools/list":
            FakeMCP.tools_calls += 1
            tools = [
                {
                    "name": "alpha",
                    "description": "第一个工具",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
            if FakeMCP.tools_calls > 1:
                tools.append(
                    {
                        "name": "beta",
                        "description": "热更新后出现的工具",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                )
            result = {"tools": tools}
            if self.path.startswith("/mcp/sse"):
                self._sse([{"jsonrpc": "2.0", "id": rid, "result": result}])
            else:
                self._json({"jsonrpc": "2.0", "id": rid, "result": result})
            return

        if method == "tools/call":
            name = (msg.get("params") or {}).get("name", "?")
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": f"called {name}"}]},
                }
            )
            return

        if method == "slow":
            time.sleep(2)
            try:
                self._json({"jsonrpc": "2.0", "id": rid, "result": {}})
            except OSError:
                pass  # 客户端已超时断开
            return

        self._json(
            {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}
        )

    def do_GET(self) -> None:  # noqa: N802
        if "text/event-stream" not in (self.headers.get("Accept") or ""):
            self._empty(406)
            return
        # 只有 manager 集成测试带这个头：单元测试里的客户端不消费通知，
        # 否则「只发一次」的标记会被前面的用例吃掉。
        if self.headers.get("X-Test-Notify") == "1" and FakeMCP.notify_once:
            FakeMCP.notify_once = False
            self._sse([
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "hello from fake"},
                },
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {},
                },
            ])
            return
        # 后续连接只保持打开、不再推送，避免触发无限重载
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for _ in range(100):
            time.sleep(0.1)

    def do_DELETE(self) -> None:  # noqa: N802
        self._empty(204)


# ============================================================ 1. JSON 传输

def test_client_json():
    client = MCPHttpClient("fake", BASE, timeout=5)
    try:
        client.start()
        assert client.server_info.get("name") == "fake-http", client.server_info
        assert client._session_id == SESSION, client._session_id

        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["alpha"], tools

        out = client.call_tool("alpha", {"x": 1})
        assert "called alpha" in out, out

        # initialized 与 tools 请求都带上了会话头
        assert any(m.get("method") == "notifications/initialized" for m in FakeMCP.seen)
        assert client.stats["requests"] >= 3, client.stats
    finally:
        client.stop()
    print("✓ HTTP JSON：initialize / 会话头 / 列工具 / 调用工具 正确")


# ============================================================ 2. SSE 传输

def test_client_sse():
    client = MCPHttpClient("fake-sse", BASE + "/sse", timeout=5)
    try:
        client.start()
        tools = client.list_tools()
        assert "alpha" in [t["name"] for t in tools], tools
    finally:
        client.stop()
    print("✓ HTTP SSE：text/event-stream 响应能被正确解析")


# ============================================================ 3. 超时与取消

def test_timeout_and_cancel():
    client = MCPHttpClient("fake-timeout", BASE, timeout=1)
    try:
        client.start()
        t0 = time.time()
        try:
            client.request("slow")
        except MCPError as e:
            assert "超时" in str(e), e
        else:
            raise AssertionError("应当超时")
        assert time.time() - t0 < 4, "超时不应等到服务端睡眠结束"
        assert client.stats["timeouts"] == 1, client.stats

        # 取消通知能发到服务端
        assert client.notify_cancelled(999, "测试取消") is True
        assert any(
            m.get("method") == "notifications/cancelled" for m in FakeMCP.seen
        ), "服务端应当收到取消通知"

        # 已取消的令牌：请求发出前就被拦下
        tok = CancellationToken()
        tok.cancel("单元测试")
        prev = set_current_token(tok)
        try:
            try:
                client.request("tools/list")
            except Cancelled:
                pass
            else:
                raise AssertionError("应当抛出 Cancelled")
        finally:
            set_current_token(prev)
    finally:
        client.stop()
    print("✓ 超时 / 取消通知 / 已取消令牌拦截 全部正确")


# ============================================================ 4. manager 集成

def _wait_until(fn, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_manager_http():
    # 前面两个用例也调过 tools/list，这里重置计数，保证首次只有 alpha
    FakeMCP.tools_calls = 0
    FakeMCP.notify_once = True
    connector_store.save({
        "servers": {
            "remote": {
                "display_name": "远程假服务",
                "transport": "http",
                "url": BASE,
                "headers": {"X-Test-Notify": "1"},
                "timeout": 5,
                "enabled": True,
                "dangerous": False,
            }
        }
    })
    reg = ToolRegistry()
    mgr = MCPManager(connector_store.config_path(), bootstrap.PACKAGE_ROOT, reg)
    try:
        mgr.load()
        item = next(s for s in mgr.snapshot()["servers"] if s["name"] == "remote")
        assert item["status"] == "ready", item
        assert item["transport"] == "http" and item["tool_count"] == 1, item

        # tools/list_changed → 0.5s 防抖后自动重载，beta 出现
        assert _wait_until(
            lambda: any(t.name == "remote__beta" for t in reg.all(source="mcp")), 6.0
        ), [t.name for t in reg.all(source="mcp")]

        # notifications/message 进入日志环
        assert _wait_until(
            lambda: any(
                "hello from fake" in line
                for s in mgr.snapshot()["servers"]
                for line in s["logs"]
            ),
            3.0,
        ), mgr.snapshot()
        item = next(s for s in mgr.snapshot()["servers"] if s["name"] == "remote")
        assert any("hello from fake" in line for line in item["logs"]), item["logs"]
    finally:
        mgr.shutdown()
    print("✓ manager：HTTP 热加载 / 工具变更自动重载 / 通知日志 全部正确")


# ============================================================ 入口

def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ma-mcp-http-test-")
    os.environ["MINI_AGENT_DATA_DIR"] = tmp
    connector_store.save({"servers": {}})

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), FakeMCP)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        test_client_json()
        test_client_sse()
        test_timeout_and_cancel()
        test_manager_http()
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("---")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
