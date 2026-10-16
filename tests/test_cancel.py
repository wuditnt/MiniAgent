"""
取消机制的专项测试 —— 全部用假客户端 / 真子进程，不花 API 费用。

覆盖 9 件事：
  1  令牌基础：check / 幂等 / on_cancel 钩子 / 注册进程被杀
  2  纯 Python 慢工具的协作式取消
  3  并行批次取消：未启动的调用快速失败、结果补齐
  4  真实子进程被整棵树杀掉（run_shell + sleep 30）
  5  流式响应中途取消（断开连接，不空转）
  6  KeyboardInterrupt 归一化成 Cancelled
  7  取消后消息序列合法（每个 tool_call 都有回复），可继续对话
  8  MCP 工具的等待循环响应取消，且把取消通知送达对端（真实 server，慢调用）
  9  没有取消发生时一切照旧（回归）
 10  取消原因（reason）透传到 UI 与 Cancelled 文案，且不谎报「已提示」

跑法：python test_cancel.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core.cancel import (  # noqa: E402
    CancellationToken,
    Cancelled,
    current_token,
    set_current_token,
)

from bootstrap import PACKAGE_ROOT as HERE


# ============================================================ 假对象工厂

def tool_call(cid: str, name: str, **args) -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def resp(content="", tool_calls=None, usage=None):
    # tool_calls 用属性风格对象（模拟 openai SDK 的返回），内部再转 dict
    tcs = [
        NS(id=t["id"], function=NS(name=t["function"]["name"],
                                   arguments=t["function"]["arguments"]))
        for t in (tool_calls or [])
    ]
    msg = NS(content=content, tool_calls=tcs or None)
    r = NS(choices=[NS(message=msg)], usage=usage or NS(
        prompt_tokens=10, completion_tokens=5, total_tokens=15))
    return r


class ScriptClient:
    """按剧本返回；每次调用记录参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_registry(*tools):
    from core.registry import ToolRegistry, tool

    reg = ToolRegistry()
    for fn in tools:
        tool(fn, registry=reg)
    return reg


# ============================================================ 1 令牌基础

def test_token_basics():
    tok = CancellationToken()
    assert not tok.cancelled()

    fired = []
    tok.on_cancel(lambda: fired.append("hook1"))
    tok.on_cancel(lambda: fired.append("hook2"))

    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(sys.platform != "win32"),
    )
    tok.register_process(p)
    t0 = time.time()
    tok.cancel()
    kill_elapsed = time.time() - t0
    try:
        tok.check()
        raise AssertionError("check() 应该抛 Cancelled")
    except Cancelled:
        pass
    tok.cancel()                       # 幂等
    tok.on_cancel(lambda: fired.append("late"))   # 已取消 → 立即执行
    p.wait(timeout=10)                 # 注册的进程必须被杀掉
    assert fired == ["hook1", "hook2", "late"], fired
    assert kill_elapsed < 3.0, f"cancel() 杀进程太慢：{kill_elapsed:.1f}s"
    print(f"✓ 令牌：check/幂等/钩子顺序/注册进程被杀（杀进程耗时 {kill_elapsed:.2f}s）")


# ============================================================ 2 协作式取消

def slow_loop(n: int = 100) -> str:
    """慢工具：循环中响应取消。"""
    tok = current_token()
    for i in range(n):
        if tok is not None:
            tok.check()
        time.sleep(0.02)
    return "done"


def test_cooperative_cancel_in_tool():
    import agent as agent_mod

    reg = make_registry(slow_loop)
    client = ScriptClient([
        resp(tool_calls=[tool_call("c1", "slow_loop", n=100)]),
        resp(content="不该走到这一步"),
    ])
    a = agent_mod.Agent(verbose=False, stream=False, parallel_tools=False,
                        client=client, registry=reg)

    def cancel_soon():
        time.sleep(0.25)
        a.interrupt()

    threading.Thread(target=cancel_soon, daemon=True).start()
    t0 = time.time()
    try:
        a.run("慢慢跑")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass
    elapsed = time.time() - t0
    assert elapsed < 3.0, f"取消太慢：{elapsed:.1f}s"
    print(f"✓ 协作式取消：慢工具在 {elapsed:.2f}s 内被掐断")


# ============================================================ 3 并行批次取消

def canceller() -> str:
    """立即触发取消。"""
    tok = current_token()
    if tok is not None:
        tok.cancel()
    return "cancelled-by-me"


def looper(n: int = 50) -> str:
    """循环检查令牌。"""
    tok = current_token()
    for i in range(n):
        if tok is not None:
            tok.check()
        time.sleep(0.02)
    return "done"


def test_parallel_batch_cancel():
    import agent as agent_mod

    reg = make_registry(canceller, looper)
    client = ScriptClient([
        resp(tool_calls=[
            tool_call("cA", "canceller"),
            tool_call("cB", "looper", n=50),
            tool_call("cC", "looper", n=50),
        ]),
        resp(content="不该走到这一步"),
    ])
    a = agent_mod.Agent(verbose=False, stream=False, parallel_tools=True,
                        max_workers=3, client=client, registry=reg)
    t0 = time.time()
    try:
        a.run("并行取消")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass
    elapsed = time.time() - t0
    assert elapsed < 3.0, f"并行批次取消太慢：{elapsed:.1f}s"

    # 消息一致性：三个 tool_call 都必须有回复
    replies = {m["tool_call_id"]: m["content"] for m in a.messages if m.get("role") == "tool"}
    assert set(replies) == {"cA", "cB", "cC"}, replies
    print(f"✓ 并行批次取消：{elapsed:.2f}s 内收场，3 个 tool_call 都补了回复")


# ============================================================ 4 真实子进程被杀

def test_subprocess_killed():
    import agent as agent_mod
    from core.registry import REGISTRY

    sleeper = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    client = ScriptClient([
        resp(tool_calls=[tool_call("c1", "run_shell", command=sleeper, timeout=60)]),
        resp(content="不该走到这一步"),
    ])
    a = agent_mod.Agent(verbose=False, stream=False, parallel_tools=False,
                        client=client, registry=REGISTRY)

    def cancel_soon():
        time.sleep(0.8)
        a.interrupt()

    threading.Thread(target=cancel_soon, daemon=True).start()
    t0 = time.time()
    try:
        a.run("睡 30 秒")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass
    elapsed = time.time() - t0
    # sleep 30 的命令在 0.8s 时被取消；若子进程没被杀，工具要等 60s 超时
    assert elapsed < 6.0, f"子进程没被及时杀掉：{elapsed:.1f}s"
    reply = [m["content"] for m in a.messages if m.get("role") == "tool"]
    assert reply and "[cancelled]" in reply[0], reply
    print(f"✓ 真实子进程：run_shell(sleep 30) 在 {elapsed:.2f}s 内被整棵树终止")


# ============================================================ 5 流式中断

def _stream_chunk(content=None):
    d = NS(content=content, reasoning_content=None, tool_calls=None)
    return NS(choices=[NS(delta=d)], usage=None)


def test_stream_cancel():
    import agent as agent_mod

    closed = []

    def make_stream(**kwargs):
        def gen():
            yield _stream_chunk("开始")
            yield _stream_chunk("继续")
            # 模拟网络卡住：等取消令牌（最多 10s，避免测试挂死），
            # 取消后再吐一帧 —— 控制权回到 agent 的循环，
            # 它在「分片之间」的检查点会发现取消并关闭流。
            tok = current_token()
            deadline = time.time() + 10
            while tok is None or not tok.cancelled():
                if time.time() > deadline:
                    return
                time.sleep(0.05)
            yield _stream_chunk("取消后的一帧，agent 不应消费它")

        class S:
            def __iter__(self):
                return gen()

            def __next__(self):
                return next(g)

            def close(self):
                closed.append(True)
                g.close()

        g = gen()
        return S()

    client = NS(chat=NS(completions=NS(create=make_stream)))
    a = agent_mod.Agent(verbose=False, stream=True, parallel_tools=False,
                        client=client, registry=make_registry())

    def cancel_soon():
        time.sleep(0.3)
        a.interrupt()

    threading.Thread(target=cancel_soon, daemon=True).start()
    t0 = time.time()
    try:
        a.run("流式中断")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass
    elapsed = time.time() - t0
    assert closed == [True], "agent 应该调用 stream.close() 断开连接"
    # 中断发生在 LLM 阶段 → 没有未回答的 tool_call，消息序列天然干净
    roles = [m["role"] for m in a.messages]
    assert roles == ["system", "user"], roles
    print(f"✓ 流式中断：{elapsed:.2f}s 内断开连接（close 被调用），消息序列保持干净")


# ============================================================ 6 KeyboardInterrupt 归一化

def test_keyboard_interrupt_normalized():
    import agent as agent_mod

    client = ScriptClient([KeyboardInterrupt()])
    a = agent_mod.Agent(verbose=False, stream=False, client=client,
                        registry=make_registry())
    try:
        a.run("ctrl-c")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass
    print("✓ KeyboardInterrupt：被归一化成 Cancelled（调用方只需接一种异常）")


# ============================================================ 7 取消后可继续对话

def canceller_raise() -> str:
    """取消并快速失败 —— 回复落成 [cancelled] 文本。"""
    tok = current_token()
    if tok is not None:
        tok.cancel()
        tok.check()
    return "x"


def test_conversation_continues_after_cancel():
    import agent as agent_mod

    reg = make_registry(canceller_raise)
    client = ScriptClient([
        resp(tool_calls=[tool_call("c1", "canceller_raise")]),
        resp(content="这是取消后的新回答"),
    ])
    a = agent_mod.Agent(verbose=False, stream=False, client=client, registry=reg)
    try:
        a.run("第一轮（会取消）")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass

    tool_msgs = [m for m in a.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and "[cancelled]" in tool_msgs[0]["content"], tool_msgs

    # 第二轮正常跑 —— 消息序列必须能直接发给 API
    answer = a.run("继续")
    assert answer == "这是取消后的新回答"
    print("✓ 取消后继续对话：tool 回复已补齐，新一轮请求序列合法")


# ============================================================ 8 MCP 取消

def test_mcp_cancel():
    from core.mcp import MCPManager
    from core.registry import ToolRegistry

    reg = ToolRegistry()
    mgr = MCPManager(os.path.join(HERE, "mcp", "mcp_servers.json"), HERE, reg)
    mgr.load()
    try:
        assert reg.get("demo__sleep") is not None, "demo server 未挂载"

        tok = CancellationToken()
        prev = set_current_token(tok)

        def cancel_soon():
            time.sleep(0.4)
            tok.cancel("测试：MCP 取消")

        threading.Thread(target=cancel_soon, daemon=True).start()
        t0 = time.time()
        out = reg.call("demo__sleep", {"ms": 8000, "tag": "cancel-test"})
        elapsed = time.time() - t0
        set_current_token(prev)
        # 真实使用路径：Cancelled 从 MCP 等待循环抛出 → registry.call 转成文本
        assert "[cancelled]" in out, out
        assert elapsed < 3.0, f"MCP 取消太慢：{elapsed:.1f}s"

        # 取消通知补全：本端不等了还不够，通知必须真的送到对端（见 test_mcp_cancel.py）
        assert mgr.cancel_stats()["cancellations"] == 1, mgr.cancel_stats()
        client = mgr.clients["demo"]
        log = None
        deadline = time.time() + 3.0
        while time.time() < deadline:      # 通知在服务端的另一个线程里处理，给点耐心
            data = json.loads(client.call_tool("cancel_log", {}))
            if data["received"]:
                log = data
                break
            time.sleep(0.05)
        assert log, f"server 从未收到 notifications/cancelled（stderr: {client.stderr_tail(5)}）"
        assert "MCP 取消" in log["received"][0]["reason"], log

        # 取消后 server 仍然可用（迟到的响应被丢弃，不影响后续调用）
        out2 = reg.call("demo__sha256", {"text": "hello"})
        assert "2cf24dba" in out2.lower(), out2   # sha256("hello") 前缀
        print(
            f"✓ MCP 取消：等待循环 {elapsed:.2f}s 内响应取消，且通知已送达对端"
            f"（server 之后仍可用）"
        )
    finally:
        mgr.shutdown()


# ============================================================ 9 回归：没有取消时照常

def quick(x: int = 1) -> str:
    """快工具。"""
    time.sleep(0.05)
    return f"ok-{x}"


def test_no_cancel_regression():
    import agent as agent_mod

    reg = make_registry(quick)
    client = ScriptClient([
        resp(tool_calls=[tool_call("c1", "quick", x=7)]),
        resp(content="完成"),
    ])
    a = agent_mod.Agent(verbose=False, stream=False, parallel_tools=True,
                        client=client, registry=reg)
    assert a.run("正常任务") == "完成"
    assert a.messages[-2]["content"] == "ok-7"
    print("✓ 回归：无取消时并行工具执行与消息回填一切照旧")


# ============================================================ 10 取消原因送达 UI

def test_cancel_reason_reaches_ui():
    """取消的另一半（对人）：说清「为什么停」，并且不重复说。

    MCP 有对端要通知，控制台前坐着的人同样是「对端」。UI 事件返回 bool、
    agent 记 last_cancel_notice_shown —— 跟 llm_delta / last_answer_streamed
    同一套约定，让 repl 与 UI 不会把同一件事打两遍。
    """
    import agent as agent_mod
    from ui import PlainUI

    seen: list[str] = []

    class RecordingUI(PlainUI):
        def __init__(self) -> None:
            super().__init__(enabled=False)

        def cancelled(self, reason: str = "") -> bool:
            seen.append(reason)
            return True

    reg = make_registry(slow_loop)
    client = ScriptClient([
        resp(tool_calls=[tool_call("c1", "slow_loop", n=100)]),
        resp(content="不该走到这一步"),
    ])
    a = agent_mod.Agent(verbose=False, stream=False, parallel_tools=False,
                        client=client, registry=reg, ui=RecordingUI())

    def cancel_soon():
        time.sleep(0.25)
        a.interrupt("测试原因：用户按了 Ctrl+C")

    threading.Thread(target=cancel_soon, daemon=True).start()
    try:
        a.run("慢慢跑")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled as e:
        assert "测试原因：用户按了 Ctrl+C" in str(e), str(e)
    assert seen and "测试原因：用户按了 Ctrl+C" in seen[0], seen
    assert a.last_cancel_notice_shown is True

    # 反向：UI 没实现 / 关闭时，不该谎报「已提示」，否则调用方会静默丢弃提示
    a2 = agent_mod.Agent(verbose=False, stream=False, client=ScriptClient([KeyboardInterrupt()]),
                         registry=make_registry(), ui=PlainUI(enabled=False))
    try:
        a2.run("ctrl-c")
        raise AssertionError("应该抛 Cancelled")
    except Cancelled:
        pass
    assert a2.last_cancel_notice_shown is False
    print("✓ 取消原因：透传到 UI 与 Cancelled 文案；UI 关闭时不谎报「已提示」")


# ============================================================ main

def main() -> int:
    tests = [
        test_token_basics,
        test_cooperative_cancel_in_tool,
        test_parallel_batch_cancel,
        test_subprocess_killed,
        test_stream_cancel,
        test_keyboard_interrupt_normalized,
        test_conversation_continues_after_cancel,
        test_mcp_cancel,
        test_no_cancel_regression,
        test_cancel_reason_reaches_ui,
    ]
    print("=== 取消机制专项测试 ===\n")
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
