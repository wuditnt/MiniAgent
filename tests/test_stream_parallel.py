"""
流式输出 + 并行工具执行的单元测试。不需要 API Key，不花任何费用，也不联网。

这两块是「感知提升最大」和「性能提升最大」的两处改造，但也是最容易写出
隐蔽 bug 的地方，所以测试重点放在失败模式上：

  流式
    · 分片到达的内容要能拼回完整回答（拼错 → 回答缺字）
    · 分片到达的工具调用要能拼回合法 JSON（拼错 → 参数不合法 / 工具名空）
    · usage 只出现在「choices 为空的统计帧」里，容易漏采
    · UI 被关掉时不能再声称「已经显示过了」，否则回答会凭空消失
    · 服务端不按 SSE 回时不能静默返回空回答

  并行
    · 结果必须按 tool_calls 的原顺序回填（协议要求一一对应）
    · 真并行要真的更快，且 max_workers 上限有效
    · exclusive 工具必须让整批退回串行
    · 危险确认永远在主线程串行做

运行：python test_stream_parallel.py
"""

from __future__ import annotations

import io
import json
import os
import queue
import sys
import threading
import time
from types import SimpleNamespace as NS

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import agent as agent_mod  # noqa: E402
import ui as ui_mod  # noqa: E402
from core.registry import ToolRegistry, tool  # noqa: E402


# ============================================================ 假流式客户端

def chunk(content=None, tool_calls=None, usage=None, reasoning=None) -> NS:
    """一帧普通 chunk：choices 里带 delta。"""
    delta = NS(content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    return NS(choices=[NS(delta=delta)], usage=usage)


def usage_chunk(prompt=10, completion=5) -> NS:
    """统计帧：choices 为空，只有 usage —— 真实服务端就是这么发的。"""
    return NS(choices=[], usage=NS(prompt_tokens=prompt, completion_tokens=completion,
                                  total_tokens=prompt + completion))


def tc_frag(index=0, tid=None, name=None, args=None) -> NS:
    """工具调用的一个分片：id 与 name 只在第一片出现，args 逐片累加。"""
    return NS(index=index, id=tid, function=NS(name=name, arguments=args))


class _Resp:
    """普通（非流式）响应对象。"""

    def __init__(self, content=None, tool_calls=None):
        msg = NS(content=content, tool_calls=tool_calls)
        self.choices = [NS(message=msg)]
        self.usage = NS(prompt_tokens=3, completion_tokens=4, total_tokens=7)


class _Client:
    """按脚本返回：列表 → 流（迭代器）；_Resp → 普通响应。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        turn = self.turns.pop(0)
        # 真实 SDK 在 stream=True 时返回的是「迭代器」，不是 list —— 这里保持一致
        return iter(turn) if isinstance(turn, list) else turn


def tc(tid: str, name: str, args: dict) -> dict:
    return {"id": tid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


# ============================================================ 假工具

def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool("slow", "会睡一会的工具，用来测量并行度。", registry=reg)
    def slow(tag: str = "", ms: int = 300) -> str:
        time.sleep(ms / 1000)
        return f"done:{tag}"

    @tool("excl", "独占工具：它一出现，整批工具都必须串行。", exclusive=True, registry=reg)
    def excl(tag: str = "") -> str:
        return f"excl:{tag}"

    @tool("boom", "会抛异常的工具。", registry=reg)
    def boom(tag: str = "") -> str:
        raise RuntimeError("炸了")

    @tool("danger", "危险工具。", dangerous=True, registry=reg)
    def danger(tag: str = "") -> str:
        return f"danger:{tag}"

    return reg


class _UI:
    """记录调用痕迹的最小 UI（故意不实现全部方法，顺便验证容错）。"""

    def __init__(self, confirm: bool = True):
        self.confirm = confirm
        self.asked: list[tuple[str, str]] = []
        self.starts: list[str] = []
        self.ends: list[str] = []
        self.batches: list[int] = []
        self.done: list[tuple[int, float, float]] = []
        self.deltas: list[tuple[str, str]] = []

    def tools_start(self, turn, items):
        self.batches.append(len(items))

    def tools_done(self, turn, count, wall, longest):
        self.done.append((count, wall, longest))

    def tool_start(self, turn, tool, args):
        self.starts.append(tool.name)

    def tool_end(self, turn, tool, result, seconds):
        self.ends.append(tool.name)

    def ask_confirm(self, tool, args):
        # 记录是在哪个线程被问的：确认必须发生在主线程
        self.asked.append((tool.name, threading.current_thread().name))
        return self.confirm

    def llm_delta(self, turn, text, kind="content"):
        self.deltas.append((kind, text))
        return kind == "content"


# ============================================================ 1. 流式聚合

def test_stream_content_is_joined():
    buf = io.StringIO()
    ui = ui_mod.PlainUI(enabled=True)
    a = agent_mod.Agent(verbose=False, ui=ui, client=_Client([
        [chunk(content="你"), chunk(content="好"), chunk(content="，世界"), usage_chunk()],
    ]))
    import contextlib
    with contextlib.redirect_stdout(buf):
        out = a.run("打个招呼")

    assert out == "你好，世界", repr(out)
    assert "你好，世界" in buf.getvalue(), "内容应该边生成边显示"
    assert a.last_answer_streamed is True, "已上屏要如实告诉调用方"
    assert a.usage["prompt_tokens"] == 10 and a.usage["completion_tokens"] == 5, a.usage
    print("✓ 流式：分片内容拼回完整回答 + 边生成边上屏 + usage 从统计帧采集")


def test_disabled_ui_must_not_claim_streamed():
    """UI 关掉时若仍声称「已显示」，回答就会凭空消失 —— 这是最容易踩的坑。"""
    a = agent_mod.Agent(verbose=False, ui=ui_mod.PlainUI(enabled=False), client=_Client([
        [chunk(content="看不见的回答"), usage_chunk()],
    ]))
    out = a.run("hi")
    assert out == "看不见的回答"
    assert a.last_answer_streamed is False, "没显示过就不能说显示过"
    print("✓ 流式：UI 关闭时 last_answer_streamed=False，调用方会补印，回答不会丢")


def test_stream_tool_call_fragments_are_merged():
    reg = make_registry()
    a = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, client=_Client([
        # 工具调用的三个分片：id/name 在第一片，arguments 被切成两段
        [chunk(tool_calls=[tc_frag(0, tid="call_1", name="slow", args="")]),
         chunk(tool_calls=[tc_frag(0, args='{"tag"')]),
         chunk(tool_calls=[tc_frag(0, args=': "x", "ms": 1}')]),
         usage_chunk()],
        [chunk(content="好了"), usage_chunk()],
    ]))
    out = a.run("跑一下")
    assert out == "好了"

    assistant = a.messages[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "slow", assistant
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"tag": "x", "ms": 1}', assistant
    assert a.messages[3]["content"] == "done:x", a.messages[3]
    assert a.messages[3]["tool_call_id"] == "call_1"
    print("✓ 流式：工具调用分片按 index 聚合（name 不丢、arguments 逐片拼接）")


def test_stream_multi_tool_calls_keep_index_order():
    reg = make_registry()
    a = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, client=_Client([
        [chunk(tool_calls=[tc_frag(0, tid="c0", name="slow", args="")]),
         chunk(tool_calls=[tc_frag(1, tid="c1", name="slow", args="")]),
         chunk(tool_calls=[tc_frag(0, args='{"tag": "a", "ms": 1}')]),
         chunk(tool_calls=[tc_frag(1, args='{"tag": "b", "ms": 1}')]),
         usage_chunk()],
        [chunk(content="两个都跑了"), usage_chunk()],
    ]))
    a.run("跑两个")
    ids = [t["id"] for t in a.messages[2]["tool_calls"]]
    assert ids == ["c0", "c1"], ids
    tools = [m["content"] for m in a.messages if m["role"] == "tool"]
    assert tools == ["done:a", "done:b"], tools
    print("✓ 流式：同一轮多个工具调用交错分片时，仍按 index 归位且顺序正确")


def test_stream_reasoning_is_shown_but_not_in_answer():
    ui = _UI()
    a = agent_mod.Agent(verbose=False, ui=ui, client=_Client([
        [chunk(reasoning="嗯…"), chunk(reasoning="先想想"), chunk(content="答案"), usage_chunk()],
    ]))
    out = a.run("想一下")
    assert out == "答案", repr(out)
    kinds = [k for k, _t in ui.deltas]
    assert kinds == ["reasoning", "reasoning", "content"], kinds
    print("✓ 流式：推理模型的 reasoning_content 只用于展示，不进回答")


def test_non_stream_response_is_tolerated_in_stream_mode():
    """假客户端忽略 stream=True 直接返回普通响应 —— 不该炸，也不该丢内容。"""
    a = agent_mod.Agent(verbose=False, ui=_UI(), client=_Client([
        _Resp(content="普通响应也能接"),
    ]))
    out = a.run("hi")
    assert out == "普通响应也能接", repr(out)
    assert a.last_answer_streamed is False
    print("✓ 流式：客户端返回非可迭代响应时自动走普通路径，不抛异常")


def test_empty_stream_falls_back_to_plain():
    """服务端「答应了流式」却一帧都不发：必须自动改用普通模式，而不是回一个空回答。"""
    client = _Client([
        [],                      # 空流（SDK 对非 SSE 响应就是这种表现）
        _Resp(content="重试成功"),  # 同一个 payload 重发（stream 已被置为 False）
    ])
    a = agent_mod.Agent(verbose=False, ui=_UI(), client=client)
    out = a.run("hi")
    assert out == "重试成功", repr(out)
    assert a.stream is False, "试过空流之后不该每轮再试"
    assert len(client.requests) == 2, "应当重发一次"
    assert client.requests[0].get("stream") is True
    assert client.requests[1].get("stream") is False
    print("✓ 流式：空流自动降级为普通模式重试（不会回一个空回答）")


# ============================================================ 2. 并行执行

def test_parallel_is_actually_faster_and_ordered():
    reg = make_registry()
    calls = [tc(f"c{i}", "slow", {"tag": f"t{i}", "ms": 300}) for i in range(3)]

    a = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, parallel_tools=True)
    t0 = time.time()
    par = a._run_tools(1, calls)
    t_par = time.time() - t0

    a2 = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, parallel_tools=False)
    t0 = time.time()
    ser = a2._run_tools(1, calls)
    t_ser = time.time() - t0

    assert [r for _tc, r in par] == ["done:t0", "done:t1", "done:t2"], par
    assert [r for _tc, r in ser] == ["done:t0", "done:t1", "done:t2"], ser
    assert t_par < 0.6, f"3×300ms 并行应该 ~0.3s，实测 {t_par:.2f}s"
    assert t_ser > 0.85, f"串行应该 ~0.9s，实测 {t_ser:.2f}s"
    print(f"✓ 并行：3×300ms 工具 {t_ser:.2f}s → {t_par:.2f}s（{t_ser / t_par:.1f}x），"
          f"且结果顺序与 tool_calls 一致")


def test_parallel_respects_max_workers():
    reg = make_registry()
    calls = [tc(f"c{i}", "slow", {"tag": f"t{i}", "ms": 400}) for i in range(3)]

    a = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg,
                        parallel_tools=True, max_workers=2)
    t0 = time.time()
    a._run_tools(1, calls)
    wall = time.time() - t0
    # 3 个任务、2 个线程 → 两批 → 约 0.8s（若忽略上限会是 0.4s）
    assert 0.65 < wall < 1.4, f"max_workers=2 应表现为两批，实测 {wall:.2f}s"
    print(f"✓ 并行：max_workers 上限生效（3 个 400ms 任务、2 线程 → {wall:.2f}s，约两批）")


def test_exclusive_tool_forces_serial_batch():
    reg = make_registry()
    calls = [
        tc("c0", "slow", {"tag": "a", "ms": 300}),
        tc("c1", "excl", {"tag": "x"}),          # 独占 → 整批退回串行
        tc("c2", "slow", {"tag": "b", "ms": 300}),
    ]
    a = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, parallel_tools=True)
    t0 = time.time()
    out = a._run_tools(1, calls)
    wall = time.time() - t0
    assert [r for _tc, r in out] == ["done:a", "excl:x", "done:b"], out
    assert wall > 0.5, f"有 exclusive 工具时不该并行，实测 {wall:.2f}s"
    print(f"✓ 并行：exclusive 工具让整批退回串行（实测 {wall:.2f}s）")


def test_ui_gets_batch_and_results():
    reg = make_registry()
    ui = _UI()
    a = agent_mod.Agent(verbose=False, ui=ui, registry=reg, parallel_tools=True)
    a._run_tools(2, [tc(f"c{i}", "slow", {"tag": f"t{i}", "ms": 120}) for i in range(3)])
    assert ui.batches == [3], ui.batches          # 并行批次只播报一次
    assert sorted(ui.ends) == ["slow", "slow", "slow"], ui.ends
    assert ui.starts == [], "并行模式下不在执行前逐个播报"
    assert len(ui.done) == 1, ui.done             # 批次收尾统计恰好一次
    count, wall, longest = ui.done[0]
    assert count == 3 and 0.1 < wall < 0.3, ui.done   # 3×120ms 并行 ≈ 0.12s，远小于 0.36s
    assert longest >= 0.1, ui.done
    print(f"✓ 并行：轨迹按「先播报批次、再逐个出结果、最后报墙钟」输出"
          f"（3 个工具一批，墙钟 {wall:.2f}s / 最长 {longest:.2f}s）")


def test_errors_do_not_break_parallel_batch():
    reg = make_registry()
    out = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, parallel_tools=True)._run_tools(
        1,
        [tc("c0", "boom", {"tag": "a"}), tc("c1", "slow", {"tag": "b", "ms": 1}),
         tc("c2", "不存在", {}), tc("c3", "slow", {"tag": "d", "ms": 1})],
    )
    texts = [r for _tc, r in out]
    assert texts[0].startswith("[error] RuntimeError"), texts[0]
    assert texts[1] == "done:b"
    assert "[error] 未知工具" in texts[2], texts[2]
    assert texts[3] == "done:d", "一个工具炸了不能连累同批的其它工具"
    assert len(out) == 4
    print("✓ 并行：单个工具抛异常 / 工具名不存在，都不影响同批其它工具与循环")


def test_denial_happens_on_main_thread_and_keeps_order():
    reg = make_registry()
    ui = _UI(confirm=False)          # 用户总是拒绝
    a = agent_mod.Agent(verbose=False, ui=ui, registry=reg, parallel_tools=True, approve=True)
    out = a._run_tools(1, [
        tc("c0", "slow", {"tag": "a", "ms": 1}),
        tc("c1", "danger", {"tag": "x"}),       # 会被拒绝
        tc("c2", "slow", {"tag": "b", "ms": 1}),
    ])
    texts = [r for _tc, r in out]
    assert texts[0] == "done:a" and texts[2] == "done:b"
    assert texts[1].startswith("[denied]"), texts[1]
    assert len(ui.asked) == 1, ui.asked
    assert ui.asked[0][1] == "MainThread", f"确认必须在主线程做，实际是 {ui.asked[0][1]}"
    print("✓ 并行：危险确认在主线程串行完成，被拒的那个单独返回 [denied]，其余照常执行")


def test_parallel_counts_usage():
    reg = make_registry()
    a = agent_mod.Agent(verbose=False, ui=_UI(), registry=reg, parallel_tools=True)
    a._run_tools(1, [tc(f"c{i}", "slow", {"tag": "t", "ms": 1}) for i in range(4)])
    assert a.usage["tool_calls"] == 4, a.usage
    print("✓ 并行：工具调用计数按调用数累加（不是按批次）")


# ============================================================ 3. UI 容错

def test_ui_missing_methods_is_tolerated():
    class Bare:
        pass

    reg = make_registry()
    a = agent_mod.Agent(verbose=False, ui=Bare(), registry=reg, client=_Client([
        [chunk(tool_calls=[tc_frag(0, tid="c0", name="slow", args='{"ms": 1}')]), usage_chunk()],
        [chunk(content="完成"), usage_chunk()],
    ]))
    assert a.run("hi") == "完成"
    print("✓ UI 容错：只实现了 0 个方法的 UI 也不会让循环崩溃")


def test_ui_with_old_signature_still_works():
    """自定义 UI 用旧签名（没有 streamed= 这个新参数）时也要能跑。"""
    seen: list[str] = []

    class OldSignatureUI:
        def llm_done(self, turn, latency):        # 旧签名：少一个 streamed
            seen.append(f"llm_done:{turn}")

        def tool_end(self, turn, tool, result, seconds):
            seen.append(f"tool_end:{tool.name}")

    reg = make_registry()
    a = agent_mod.Agent(verbose=False, ui=OldSignatureUI(), registry=reg, client=_Client([
        [chunk(tool_calls=[tc_frag(0, tid="c0", name="slow", args='{"ms": 1}')]), usage_chunk()],
        [chunk(content="完成"), usage_chunk()],
    ]))
    assert a.run("hi") == "完成"
    assert "llm_done:1" in seen and "tool_end:slow" in seen, seen
    print("✓ UI 容错：旧签名的 llm_done(turn, latency) 也能被调用（自动去掉新关键字参数）")


def test_plain_ui_stream_writes_and_closes_line():
    buf = io.StringIO()
    ui = ui_mod.PlainUI(enabled=True)
    import contextlib
    with contextlib.redirect_stdout(buf):
        assert ui.llm_delta(1, "abc") is True
        assert ui.llm_delta(1, "def") is True
        ui.flush_stream()
    assert buf.getvalue() == "abcdef\n", repr(buf.getvalue())
    # 关闭一次之后不该再补换行
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        ui.flush_stream()
    assert buf2.getvalue() == ""
    print("✓ UI：流式增量直接落 stdout，flush_stream 负责收尾换行且不会重复补")


# ============================================================ 4. MCP 并发路由

def test_mcp_responses_route_by_id():
    """并发调用的核心：响应必须按 id 精确投递，不能「取到了不是我的就丢掉」。"""
    from core.mcp import MCPStdioClient

    c = MCPStdioClient("fake", "python")
    q1: queue.Queue = queue.Queue()
    q2: queue.Queue = queue.Queue()
    c._waiters[1] = q1
    c._waiters[2] = q2

    # 故意乱序到达，并且夹一条通知
    c._dispatch({"jsonrpc": "2.0", "id": 2, "result": {"v": 2}})
    c._dispatch({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
    c._dispatch({"jsonrpc": "2.0", "id": 1, "result": {"v": 1}})

    assert q1.get_nowait()["result"] == {"v": 1}, "1 号请求必须拿到 1 号响应"
    assert q2.get_nowait()["result"] == {"v": 2}, "2 号请求必须拿到 2 号响应"
    assert c._rx.qsize() == 1, "无人认领的消息（通知）才进公共队列"
    print("✓ MCP：乱序到达的响应按 id 精确投递，通知不占用响应通道（并发不串话）")


def test_mcp_concurrent_requests_do_not_timeout():
    """对真实 MCP server 发并发请求：修复前会随机超时，修复后必须全部成功。"""
    from core.mcp import MCPManager
    from core.registry import ToolRegistry

    reg = ToolRegistry()
    mgr = MCPManager(os.path.join(HERE, "mcp", "mcp_servers.json"), HERE, reg)
    mgr.load()
    try:
        client = mgr.clients.get("demo")
        assert client is not None, "demo server 应该就绪"
        out: dict[int, str] = {}
        errors: list[str] = []

        def work(i: int) -> None:
            try:
                out[i] = client.call_tool("sleep", {"ms": 250, "tag": f"t{i}"})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{i}: {type(e).__name__}: {e}")

        t0 = time.time()
        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - t0

        assert not errors, errors
        assert len(out) == 4
        for i in range(4):
            assert f"tag=t{i}" in out[i], f"{i} 号调用拿到了别人的结果：{out[i]}"
        assert wall < 1.0, f"4×250ms 应并发完成（~0.25s），实测 {wall:.2f}s"
        print(f"✓ MCP：4 个并发请求 {wall:.2f}s 全部成功、各自拿到自己的结果")
    finally:
        mgr.shutdown()


# ============================================================ 入口

if __name__ == "__main__":
    print("工作区:", HERE)
    print("--- 流式 ---")
    test_stream_content_is_joined()
    test_disabled_ui_must_not_claim_streamed()
    test_stream_tool_call_fragments_are_merged()
    test_stream_multi_tool_calls_keep_index_order()
    test_stream_reasoning_is_shown_but_not_in_answer()
    test_non_stream_response_is_tolerated_in_stream_mode()
    test_empty_stream_falls_back_to_plain()
    print("--- 并行 ---")
    test_parallel_is_actually_faster_and_ordered()
    test_parallel_respects_max_workers()
    test_exclusive_tool_forces_serial_batch()
    test_ui_gets_batch_and_results()
    test_errors_do_not_break_parallel_batch()
    test_denial_happens_on_main_thread_and_keeps_order()
    test_parallel_counts_usage()
    print("--- UI 容错 ---")
    test_ui_missing_methods_is_tolerated()
    test_ui_with_old_signature_still_works()
    test_plain_ui_stream_writes_and_closes_line()
    print("--- MCP 并发 ---")
    test_mcp_responses_route_by_id()
    test_mcp_concurrent_requests_do_not_timeout()
    print("---")
    print("全部通过 ✅")
