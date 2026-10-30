"""
子智能体的契约测试：不花 API 费用，用假客户端固定「模型会返回什么」。

这些用例是 subagent.py 的规格说明 —— 改实现前先看它们，尤其是这四条：
  · 子代理只能看到任务包（没有主对话历史）；
  · 工具表是只读子集（没有写子集、没有提问、没有递归派发）；
  · 结论超长会被截断并标注（否则「隔离」等于没做）；
  · 父任务取消要传导成 Cancelled，而超时 / 模型异常只降级成失败文本。
"""

from __future__ import annotations

import time

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

import agent as agent_mod  # noqa: E402
from core import cancel, subagent  # noqa: E402
from core.registry import REGISTRY, Tool, ToolRegistry  # noqa: E402


# ---------------------------------------------------------------- 假客户端

class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, i, name, arguments):
        self.id = f"call_{i}"
        self.type = "function"
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None, usage=None):
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})]
        self.usage = msg.usage


class _Scripted:
    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._msgs.pop(0))


class _StubClient:
    def __init__(self, msgs):
        self.chat = type("Chat", (), {"completions": _Scripted(msgs)})()

    def close(self):
        pass


# ---------------------------------------------------------------- 测试

def test_task_package_only():
    """子代理只收到 system + 任务包，看不到任何主对话历史。"""
    client = _StubClient([_Msg(content="结论：可以。")])
    res = subagent.run_subagent("盘点 docs 目录里的过期文档", client=client)

    assert res.ok and res.text == "结论：可以。", res
    assert res.turns == 1, res

    sent = client.chat.completions.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"], [m["role"] for m in sent]
    assert sent[1]["content"] == "盘点 docs 目录里的过期文档"
    # system 是子代理自己的提示词，不是主提示词（也不应包含主提示词原文）
    assert "子任务执行者" in sent[0]["content"], sent[0]["content"][:80]
    assert agent_mod.DEFAULT_SYSTEM_PROMPT not in sent[0]["content"]
    print("✓ 上下文：只有任务包，不带主对话历史")


def test_readonly_tool_subset():
    """工具表是只读白名单：没有写入 / 执行 / 提问 / 递归派发，也不动全局表。"""
    reg = subagent.build_registry()
    names = set(reg.names())

    assert {"read_file", "grep", "web_search"} <= names, sorted(names)[:10]
    for bad in ("write_file", "append_to_file", "run_shell", "run_python",
                "ask_user", "request_approval", "remember", "search_tools",
                "git_commit", "kill_process", "delegate"):
        assert bad not in names, f"子代理不该拿到 {bad}"
    assert "delegate" in REGISTRY.names(), "delegate 本身应注册在主工具表里"
    # 克隆表，不改全局
    assert "write_file" in REGISTRY.names(), "build_registry 不应从全局表删工具"

    # 来源表里即使有 delegate，也必须被白名单挡掉（防止递归派发）
    src = ToolRegistry()
    src.add(Tool(name="delegate", description="派发子任务",
                 parameters={"type": "object", "properties": {}}, func=lambda: ""))
    src.add(Tool(name="read_file", description="读文件",
                 parameters={"type": "object", "properties": {}}, func=lambda: ""))
    sub = subagent.build_registry(src)
    assert sub.names() == ["read_file"], sub.names()
    print("✓ 工具：只读白名单，无写子集 / 无提问 / 无递归")


def test_request_tools_match_registry():
    """请求体里的工具必须与传入的工具表一致（不能偷偷多带）。"""
    reg = subagent.build_registry()
    client = _StubClient([_Msg(content="好。")])
    subagent.run_subagent("看看项目结构", client=client, registry=reg)

    sent = {s["function"]["name"] for s in client.chat.completions.calls[0]["tools"]}
    assert sent == set(reg.names()), f"多带：{sorted(sent - set(reg.names()))}"
    print("✓ 工具：请求体与工具表一致")


def test_result_truncated():
    """结论超长要截断并标注：把过程搬回来的子代理等于没隔离。"""
    long_text = "结" * (subagent.MAX_RESULT_CHARS + 500)
    res = subagent.run_subagent("写一份很长的报告", client=_StubClient([_Msg(content=long_text)]))

    assert res.ok, res
    assert res.truncated is True
    assert len(res.text) <= subagent.MAX_RESULT_CHARS + 80, len(res.text)
    assert "已截断" in res.text
    print("✓ 结果：超长结论被截断并标注")


def test_empty_task_rejected_without_model_call():
    client = _StubClient([])
    res = subagent.run_subagent("   ", client=client)
    assert not res.ok and "为空" in res.text, res
    assert client.chat.completions.calls == [], "空任务不该发起模型调用"
    print("✓ 入参：空任务直接拒绝，不浪费一次调用")


def test_parent_already_cancelled():
    """父任务已停止：连模型都不请求，直接抛 Cancelled（按取消处理）。"""
    token = cancel.CancellationToken()
    token.cancel("用户点了停止")
    client = _StubClient([])
    try:
        subagent.run_subagent("跑个长任务", client=client, parent_token=token)
    except cancel.Cancelled:
        pass
    else:
        raise AssertionError("父任务已取消时应当抛 Cancelled")
    assert client.chat.completions.calls == []
    print("✓ 取消：父任务已停止时直接中止")


def test_cancel_during_run_propagates():
    """子代理执行途中父任务被取消 → 子代理立刻收手并抛 Cancelled。"""
    token = cancel.CancellationToken()

    class _Cancelling:
        def __init__(self):
            self.calls = 0
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            self.calls += 1
            token.cancel("用户在子任务执行中点了停止")
            return _Resp(_Msg(content="迟到的回答"))

        def close(self):
            pass

    client = _Cancelling()
    try:
        subagent.run_subagent("跑个长任务", client=client, parent_token=token)
    except cancel.Cancelled:
        pass
    else:
        raise AssertionError("父任务取消后子代理应当抛 Cancelled")
    assert client.calls == 1
    print("✓ 取消：执行途中父任务停止会传导给子代理")


def test_timeout_returns_failure_text():
    """超时由 watchdog 中断，返回可读失败文本，不把异常抛给父任务。"""
    class _Hangs:
        """一直挂到被取消为止（不是固定 sleep，避免机器慢时误判）。"""

        def __init__(self):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            token = cancel.current_token()
            deadline = time.time() + 2.0
            while time.time() < deadline and not (token and token.cancelled()):
                time.sleep(0.01)
            return _Resp(_Msg(content="太晚了"))

        def close(self):
            pass

    res = subagent.run_subagent("慢慢查", client=_Hangs(), timeout=0.05)
    assert not res.ok and "超时" in res.text, res
    print("✓ 超时：中断并返回失败文本")


def test_model_error_degrades():
    """模型/网关报错 → 结构化失败文本，父任务可以据此换路子。"""
    class _Boom:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            raise RuntimeError("网关 502 Bad Gateway")

        def close(self):
            pass

    res = subagent.run_subagent("查一下", client=_Boom(), timeout=5)
    assert not res.ok and "[error]" in res.text, res
    assert "502" in res.text, res
    print("✓ 失败：模型异常降级为文本，不炸掉父任务")


def test_should_delegate_guards():
    """委派前的硬约束：太笼统、深度 ≥1 一律否决；上下文吃紧只给理由不否决。"""
    ok, why = subagent.should_delegate("看看")
    assert not ok and "笼统" in why, why

    ok, why = subagent.should_delegate("把 docs 下的过期文档整理出来", depth=1)
    assert not ok and "深度" in why, why

    ok, why = subagent.should_delegate("读十个文件后给出结论", context_chars=900, context_limit=1000)
    assert ok and "上下文" in why, why

    ok, why = subagent.should_delegate("读十个文件后给出结论", context_chars=100, context_limit=1000)
    assert ok and why == "", why
    print("✓ 规则：只否决明显不该拆的，上下文吃紧时给出理由")


def test_delegate_skips_vague_task():
    """delegate 工具拿到笼统任务时直接回 [skipped]，不发起任何模型调用。"""
    out = subagent.delegate("看看")
    assert out.startswith("[skipped]") and "笼统" in out, out
    print("✓ 规则：笼统任务不会派出去")


def test_delegate_summarises_result():
    """delegate 把子任务结果转成工具文本，并把父 token / 配置传给子任务。"""
    seen: dict = {}

    def _fake(task, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        return subagent.SubagentResult(True, "结论：3 个文件过期。", 4, False)

    original = subagent.run_subagent
    token = cancel.CancellationToken()
    prev = cancel.set_current_token(token)
    subagent.run_subagent = _fake
    try:
        out = subagent.delegate("把 docs 下所有文档的过期时间整理成结论")
    finally:
        subagent.run_subagent = original
        cancel.set_current_token(prev)

    assert out.startswith("[ok] 子任务完成，用时 4 轮"), out
    assert "3 个文件过期" in out, out
    assert seen["task"] == "把 docs 下所有文档的过期时间整理成结论"
    assert seen["parent_token"] is token, "父任务令牌没有传下去，取消就传导不了"
    assert seen["max_turns"] >= 1 and seen["timeout"] > 0, seen
    print("✓ 工具：结果转文本，父令牌与子任务上限正确传入")


def test_subagent_setting_disables_tool():
    """总开关关闭时，delegate 不进会话工具表（模型看不到就不会调）。"""
    from core import settings as settings_mod
    from core.webui import _session_registry

    original_get = settings_mod.get
    settings_mod.get = lambda key, *args: (
        False if key == "subagent_enabled" else original_get(key)
    )
    try:
        reg = _session_registry("agent", "auto", True, profile="standard")
    finally:
        settings_mod.get = original_get

    assert reg.get("delegate") is None, "关闭后不该再暴露 delegate"
    # 恢复配置后重新克隆，工具回来
    assert _session_registry("agent", "auto", True, profile="standard").get("delegate") is not None
    print("✓ 开关：关闭后 delegate 不进会话工具表")


def test_delegate_nudge_stages():
    """委派提醒：没有 delegate 时不说；6 次提醒一次；12 次再提醒一次（措辞更强）；之后不再提醒。"""
    first, second = agent_mod.Agent._DELEGATE_NUDGE_AT

    no_delegate = agent_mod.Agent(
        verbose=False, client=_StubClient([]), registry=subagent.build_registry()
    )
    no_delegate._tool_calls_this_run = 20
    assert no_delegate._delegate_nudge() == "", "工具表里没有 delegate 时提醒没有意义"

    from core.webui import _session_registry

    reg = _session_registry("agent", "ask", False, profile="standard")
    a = agent_mod.Agent(verbose=False, client=_StubClient([]), registry=reg)
    a._tool_calls_this_run = first - 1
    assert a._delegate_nudge() == "", "未到阈值不该提醒"
    a._tool_calls_this_run = first
    one = a._delegate_nudge()
    assert "delegate" in one and "次工具" in one, one
    assert a._delegate_nudge() == "", "第一次之后要到第二个阈值才再说"

    a._tool_calls_this_run = second
    two = a._delegate_nudge()
    assert "还没有委派" in two and two != one, two
    assert a._delegate_nudge() == "", "两次提醒之后不再重复"
    print("✓ 提醒：两次阈值，措辞递进，之后不重复")


def test_nudge_injected_after_many_tool_calls():
    """连续多轮工具调用后，请求末尾会依次出现两次委派提醒。"""
    from core.webui import _session_registry

    reg = _session_registry("agent", "ask", False, profile="standard")
    rounds = agent_mod.Agent._DELEGATE_NUDGE_AT[-1]
    script = [_Msg(tool_calls=[_TC(i, "now", "{}")]) for i in range(1, rounds + 1)]
    script.append(_Msg(content="完成。"))

    a = agent_mod.Agent(
        verbose=False, client=_StubClient(script), registry=reg, max_turns=rounds + 2
    )
    out = a.run("查几次时间")
    assert out == "完成。", out

    calls = a._client.chat.completions.calls
    nudged = [
        c for c in calls
        if (c.get("messages") or [{}])[-1].get("role") == "system"
        and "delegate" in ((c["messages"][-1].get("content")) or "")
    ]
    assert len(nudged) == 2, f"应注入两次委派提醒，实际 {len(nudged)} 次"
    assert len(nudged[0]["messages"][-1]["content"]) != len(nudged[1]["messages"][-1]["content"])
    print("✓ 提醒：两次提醒都真的进了请求末尾")


if __name__ == "__main__":
    test_task_package_only()
    test_readonly_tool_subset()
    test_request_tools_match_registry()
    test_result_truncated()
    test_empty_task_rejected_without_model_call()
    test_parent_already_cancelled()
    test_cancel_during_run_propagates()
    test_timeout_returns_failure_text()
    test_model_error_degrades()
    test_should_delegate_guards()
    test_delegate_skips_vague_task()
    test_delegate_summarises_result()
    test_subagent_setting_disables_tool()
    test_delegate_nudge_stages()
    test_nudge_injected_after_many_tool_calls()
    print("---")
    print("全部通过 ✅")
