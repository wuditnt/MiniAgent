"""
端到端管线验证：用一个「本地 OpenAI 兼容 mock 服务」把 Agent 完整跑一遍。

与 test_loop.py 的区别：
  test_loop.py 注入的是假客户端对象（不经过网络）；
  本脚本起一个真实的 HTTP 服务，走 openai SDK 的完整序列化/反序列化/重试链路。
  这样「除了 API Key 之外」的所有环节都被真实验证过。

本 mock 同时支持两种响应形态：
  · stream=false → 一次性 JSON
  · stream=true  → text/event-stream，并且**故意把工具调用和参数拆成分片**，
                   以此验证客户端的增量聚合逻辑（这是流式模式最容易写错的地方）

运行：python test_http_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 必须在导入 tools / agent 之前设置环境变量（WORKSPACE 在导入时确定）----
import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

os.environ["MINI_AGENT_WORKSPACE"] = HERE
PORT = 8771

# 隔离模型配置：若本机 web_data/models.json 里有用户保存的服务商，
# 会打到真实接口。这里指到临时文件，并造一条指向本文件 mock 服务的记录。
import tempfile  # noqa: E402

import core.model_config as _model_config  # noqa: E402

_model_config._MODELS_FILE = os.path.join(
    tempfile.mkdtemp(prefix="miniagent-e2e-"), "models.json"
)
_mock = _model_config.create_entry(
    provider="custom",
    name="mock",
    base_url=f"http://127.0.0.1:{PORT}/v1",
    api_key="mock-key-not-real",
    model="mock-gpt",
)
_model_config.set_active(_mock["id"])

# ---------------------------------------------------------------- mock 模型策略
# 一个「确定性脚本」：根据已发生的工具调用次数，决定下一步调哪个工具。
# 真实模型是概率性的，但这里我们只验证管线。

SCRIPT = [
    ("list_dir", {"path": "demo_case/inbox"}),
    ("read_file", {"path": "demo_case/inbox/sales_gbk.csv"}),
    ("read_file", {"path": "demo_case/inbox/sales_utf8bom.csv"}),
    ("run_python", {"code": (
        "import re,zlib,collections\n"
        "raw=open('demo_case/inbox/packet.bin','rb').read()\n"
        "data=zlib.decompress(raw)\n"
        "print('解压后字节:',len(data))\n"
        "c=collections.Counter(re.findall(rb'[A-Za-z0-9]{10,}',data))\n"
        "for k,v in c.most_common():\n"
        "    if v>=2: print(f'  x{v:3d}  {k.decode()}')\n"
    )}),
    ("read_file", {"path": "demo_case/inbox/logo.png"}),
]

FINAL_TEXT = (
    "【mock 模型】管线验证完成：5 次工具调用全部走通真实 HTTP 往返，"
    "工具结果已正确回填到消息历史。"
)
MOCK_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
CREATED = 1730000000

REQUEST_LOG: list[dict] = []
FAILURES: list[str] = []


def next_action(messages: list[dict]) -> tuple[str | None, str]:
    """返回 (工具名, 参数JSON字符串)；工具名为 None 表示给出最终回答。"""
    n = sum(1 for m in messages if m.get("role") == "tool")
    if n < len(SCRIPT):
        name, args = SCRIPT[n]
        return name, json.dumps(args, ensure_ascii=False)
    return None, ""


def _pieces(text: str, size: int):
    """把字符串切成小片，用来模拟流式的分片到达。"""
    return [text[i: i + size] for i in range(0, len(text), size)]


def _chunk(model: str, delta: dict, finish=None, usage=None) -> dict:
    obj = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": CREATED,
        "model": model,
        "choices": [] if (usage is not None and not delta) else [
            {"index": 0, "delta": delta, "finish_reason": finish}
        ],
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


def sse_frames(model: str, content: str | None, tool_calls: list | None,
               finish: str, usage: dict | None) -> list[dict]:
    """构造一整套流式帧：内容/工具调用分片 → 收尾帧 → 可选 usage 帧。"""
    frames: list[dict] = []
    if content:
        for piece in _pieces(content, 8):
            frames.append(_chunk(model, {"role": "assistant", "content": piece}))
        frames.append(_chunk(model, {}, finish))
    else:
        for i, tc in enumerate(tool_calls or []):
            # 第一片带 id 与函数名，参数为空
            frames.append(_chunk(model, {"role": "assistant", "tool_calls": [
                {"index": i, "id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"], "arguments": ""}}
            ]}))
            # 参数分多个片到达 —— 客户端必须拼接，不能只取最后一片
            for frag in _pieces(tc["function"]["arguments"], 7):
                frames.append(_chunk(model, {"tool_calls": [
                    {"index": i, "function": {"arguments": frag}}
                ]}))
            frames.append(_chunk(model, {}, finish))
    if usage is not None:
        frames.append(_chunk(model, {}, None, usage))   # 纯统计帧：choices 为空
    return frames


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音默认日志
        pass

    # ------------------------------------------------------------ 响应工具

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _sse(self, frames: list[dict]) -> None:
        buf = []
        for f in frames:
            buf.append("data: " + json.dumps(f, ensure_ascii=False) + "\n\n")
        buf.append("data: [DONE]\n\n")
        self._send(200, "".join(buf).encode("utf-8"), "text/event-stream")

    # ------------------------------------------------------------ 主处理

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        msgs = body.get("messages", [])
        model = body.get("model", "mock-gpt")
        want_stream = bool(body.get("stream"))
        has_opts = bool(body.get("stream_options"))

        # 用一个特殊模型名模拟「不认识 stream_options」的服务端（真实世界里很常见）
        if has_opts and model == "mock-nostreamopts":
            FAILURES.append("400 stream_options")
            self._json({"error": {
                "message": "Unrecognized request argument supplied: stream_options",
                "type": "invalid_request_error", "code": None,
            }}, code=400)
            return

        # 模拟「答应了流式却回 JSON」的网关：客户端应当能自己兜回来
        ignore_stream = (model == "mock-ignores-stream")

        name, args = next_action(msgs)
        tools_seen = [m for m in msgs if m.get("role") == "tool"]
        REQUEST_LOG.append({
            "n_messages": len(msgs),
            "n_tool_msgs": len(tools_seen),
            "tools_schema_count": len(body.get("tools") or []),
            "model": model,
            "stream": want_stream,
            "stream_options": has_opts,
            "decision": name or "<final answer>",
        })

        if name is None:
            content, tool_calls, finish = FINAL_TEXT, None, "stop"
        else:
            content, tool_calls = None, [{
                "id": f"call_{len(tools_seen) + 1}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }]
            finish = "tool_calls"

        if want_stream and not ignore_stream:
            usage = MOCK_USAGE if has_opts else None
            self._sse(sse_frames(model, content, tool_calls, finish, usage))
            return

        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._json({
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": CREATED,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": MOCK_USAGE,
        })

    def do_GET(self):  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


# ---------------------------------------------------------------- 三次运行

def run_agent(model: str, stream: bool, parallel: bool, label: str) -> tuple[bool, dict]:
    """跑一次 Agent，返回 (是否通过, 观测数据)。"""
    REQUEST_LOG.clear()
    from agent import Agent

    a = Agent(model=model, verbose=True, stream=stream, parallel_tools=parallel)
    t0 = time.time()
    out = a.run("分析 demo_case/inbox 目录里的文件")
    wall = time.time() - t0

    n_tools = len(a.registry.schemas())
    ok = (
        len(REQUEST_LOG) >= len(SCRIPT) + 1
        and all(r["tools_schema_count"] == n_tools for r in REQUEST_LOG)
        and REQUEST_LOG[-1]["decision"] == "<final answer>"
        and len(out) > 20                       # 流式拼回来的回答不能是空的
        and a.usage["tool_calls"] == len(SCRIPT)  # 5 次工具调用一次不少
    )
    print(f"最终回答（{len(out)} 字符）: {out[:80]}{'…' if len(out) > 80 else ''}")
    print(f"统计: LLM {a.usage['llm_calls']} 次 | 工具 {a.usage['tool_calls']} 次 | "
          f"tokens 输入 {a.usage['prompt_tokens']} + 输出 {a.usage['completion_tokens']} "
          f"= {a.usage['total_tokens']} | 墙钟 {wall:.1f}s")
    print(f"流式：请求 {sum(1 for r in REQUEST_LOG if r['stream'])} / "
          f"{len(REQUEST_LOG)} 次走了 stream=true")
    print("管线验证:", "通过 ✓" if ok else "未通过 ✗", f"[{label}]")
    return ok, {"wall": wall, "log": list(REQUEST_LOG), "usage": dict(a.usage)}


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[mock] OpenAI 兼容服务已启动 http://127.0.0.1:{PORT}/v1\n")

    import tools

    print("工作区:", tools.WORKSPACE)
    print("暴露给模型的工具数:", len(tools.schemas()))
    print("=" * 70)

    results: dict[str, bool] = {}

    # ① 流式 + 并行（默认形态）
    print("\n### ① 流式输出 + 并行工具（默认）")
    ok1, info1 = run_agent("mock-gpt", stream=True, parallel=True, label="流式+并行")
    results["流式+并行"] = ok1
    assert all(r["stream"] for r in REQUEST_LOG), "应全部走流式"
    assert all(r["stream_options"] for r in REQUEST_LOG), "应带 stream_options 以采集 usage"
    assert info1["usage"]["prompt_tokens"] == MOCK_USAGE["prompt_tokens"] * len(REQUEST_LOG)
    print("  ✓ 全部请求走 SSE，且 usage 从流式帧里正确采集")

    print("\n### ② 服务端不认识 stream_options → 应自动去掉后重试")
    print("=" * 70)
    ok2, _ = run_agent("mock-nostreamopts", stream=True, parallel=True, label="stream_options 降级")
    results["stream_options 降级"] = ok2
    assert FAILURES, "mock 应当记录到一次 400"
    assert not any(r["stream_options"] for r in REQUEST_LOG), "降级后不应再带 stream_options"
    assert all(r["stream"] for r in REQUEST_LOG), "仍然应该是流式"
    print("  ✓ 触发 400 后自动去参重试，仍然是流式，任务照常完成")

    print("\n### ③ 网关忽略 stream 参数 → 应自动退回普通模式")
    print("=" * 70)
    ok3, info3 = run_agent("mock-ignores-stream", stream=True, parallel=True, label="空流降级")
    results["空流降级"] = ok3
    first, second = REQUEST_LOG[0], REQUEST_LOG[1]
    assert first["stream"] and not second["stream"], "第一次应为流式、重试应为普通"
    print("  ✓ 首个请求 stream=true 但收到空流，第二个请求自动改用 stream=false")

    print("\n### ④ 显式关闭流式与并行（回归）")
    print("=" * 70)
    ok4, info4 = run_agent("mock-gpt", stream=False, parallel=False, label="全同步")
    results["全同步"] = ok4
    assert not any(r["stream"] for r in REQUEST_LOG), "不应出现流式请求"
    print("  ✓ 传统模式行为不变（改这两块没有破坏原有链路）")

    # ---------------------------------------------------------- 并行加速实测
    print("\n" + "=" * 70)
    print("### 并行 vs 串行：实测墙钟差")
    from agent import Agent

    def timed(parallel: bool) -> float:
        from core.mcp import MCPManager
        from core.registry import ToolRegistry

        reg = ToolRegistry()
        mgr = MCPManager(os.path.join(HERE, "mcp", "mcp_servers.json"), HERE, reg)
        mgr.load()
        try:
            a = Agent(verbose=False, stream=False, parallel_tools=parallel,
                      max_workers=4, registry=reg)
            t = time.time()
            out = a._run_tools(3, [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "demo__sleep",
                              "arguments": json.dumps({"ms": 500, "tag": f"t{i}"})}}
                for i in range(4)
            ])
            wall = time.time() - t
            tags = sorted(r.split("tag=")[1].split(" ")[0] for _tc, r in out)
            assert tags == ["t0", "t1", "t2", "t3"], f"并发调用串话：{tags}"
            return wall
        finally:
            mgr.shutdown()

    t_serial = timed(False)
    t_par = timed(True)
    speed = t_serial / t_par if t_par else 0
    print(f"  4 个 sleep(500ms) 的 MCP 工具调用：串行 {t_serial:.2f}s → 并行 {t_par:.2f}s"
          f"（加速 {speed:.1f}x）")
    assert t_par < t_serial * 0.6, "并行应当明显更快"
    assert speed > 2.0, f"加速比过低：{speed:.2f}"
    print("  ✓ MCP 并发无串话（4 个 tag 各自对应），且真实加速")

    srv.shutdown()
    print("\n" + "=" * 70)
    for k, v in results.items():
        print(f"  {k:22s} {'通过 ✓' if v else '未通过 ✗'}")
    ok = all(results.values())
    print("\n管线验证:", "全部通过 ✓" if ok else "有未通过项 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
