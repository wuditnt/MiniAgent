"""
长任务回归 —— 免费用例（假客户端 + 假工具，不碰网络、不烧 token）。

覆盖「一个会话跑很多轮」时的整体链路，而 test_loop.py 只覆盖单点行为：
  · 30 轮工具链完整回填，messages 里 tool_calls / tool 结果始终成对；
  · 长链中途超限时，较早工具结果被替换成占位符，最近两轮保持原文；
  · 轮数用尽（20 轮）时另起一条 assistant 收尾，不改写任何已结束的回答；
  · 轮次预算提醒只出现在最后 30% 轮，且不落盘；
  · 同一会话连续 6 轮用户输入，历史滚动摘要后仍保留 tool_calls 配对；
  · 长链执行到一半被取消，整轮以 Cancelled 结束，不会继续跑完剩余轮次。
"""

from __future__ import annotations

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

import agent as agent_mod  # noqa: E402
from core import events  # noqa: E402
from core.cancel import Cancelled, current_token  # noqa: E402


# ---------------------------------------------------------------- 假客户端

class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, i, name, arguments="{}"):
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
    """按脚本依次返回；脚本耗尽后再被调用视为失败。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._msgs:
            raise RuntimeError("脚本已耗尽：长任务比预期多跑了一轮")
        return _Resp(self._msgs.pop(0))


class _StubClient:
    def __init__(self, msgs):
        self.chat = type("Chat", (), {"completions": _Scripted(msgs)})()


def _tool_turns(n: int, name: str = "fake_read") -> list[_Msg]:
    """n 轮「只调工具、不给正文」的模型行为。"""
    return [_Msg(tool_calls=[_TC(i, name)]) for i in range(1, n + 1)]


def _registry(name: str, func):
    """最小假工具表：避免长链测试真读写文件。"""
    from core.registry import Tool, ToolRegistry

    reg = ToolRegistry()
    reg.add(Tool(name=name, description="fake long-task read",
                 func=func, parameters={"type": "object", "properties": {}}, dangerous=False))
    return reg


def _assert_pairs(messages) -> None:
    """tool 结果必须始终有对应的 tool_calls，裁剪/摘要都不能拆散配对。"""
    ids = {c["id"] for m in messages for c in (m.get("tool_calls") or [])}
    for m in messages:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in ids, f"孤儿 tool 结果：{m['tool_call_id']}"


# ---------------------------------------------------------------- 用例

def test_long_chain_30_turns_keeps_all_results():
    """30 轮工具链：每一步的结果都回填，最终答案正常返回。"""
    script = _tool_turns(30) + [_Msg(content="长任务完成：30 步全部执行完毕。")]
    a = agent_mod.Agent(
        verbose=False, client=_StubClient(script), max_turns=31,
        max_context_chars=10_000_000,
        registry=_registry("fake_read", lambda **kw: "[ok] 第 N 步读取完成"),
    )
    out = a.run("连续执行 30 个读取步骤")

    assert out == "长任务完成：30 步全部执行完毕。", out
    # system + user + (assistant + tool) × 30 + 最终 assistant
    assert len(a.messages) == 2 + 30 * 2 + 1, len(a.messages)
    _assert_pairs(a.messages)
    assert sum(1 for m in a.messages if m.get("role") == "tool") == 30
    assert a.usage["llm_calls"] == 31, a.usage
    assert not any(m.get("_injected") == "max_turns_wrapup" for m in a.messages)
    print("✓ 长链：30 轮工具结果全部回填，配对完整，无触顶收尾")


def test_long_chain_trims_old_tool_results_only():
    """长链中途超限：只替换「较老」的工具结果，最近两轮保持原文。"""
    big = "R" * 1200
    script = _tool_turns(12) + [_Msg(content="12 步完成，早期结果已裁剪。")]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script), max_turns=13,
                        registry=_registry("fake_read", lambda **kw: big))
    # 以「初始上下文 + 7.6k」为阈值：system 提示本身占一大截，不能写死绝对值
    a.max_context_chars = a._context_chars() + 7600

    out = a.run("执行 12 个长输出步骤")

    assert out == "12 步完成，早期结果已裁剪。", out
    trimmed = [m for m in a.messages if m.get("role") == "tool" and m.get("_trimmed")]
    assert trimmed, "长链应触发第一级裁剪（老工具结果换占位符）"
    assert all(m["content"] == events.TRIMMED_PLACEHOLDER for m in trimmed)
    # 最近 6 条（约两轮）是保护窗口，不能被换掉
    recent = [m for m in a.messages[-6:] if m.get("role") == "tool"]
    assert recent and all(not m.get("_trimmed") for m in recent)
    _assert_pairs(a.messages)
    print(f"✓ 长链裁剪：{len(trimmed)} 条老工具结果换成占位符，最近两轮保留原文")


def test_long_chain_hits_max_turns_with_wrapup():
    """20 轮用尽：另起一条 assistant 收尾，旧回答保持不变。"""
    usage = type("U", (), {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12})()
    script = _tool_turns(20) + [_Msg(content="已完成 20 步，剩余步骤受轮次限制。", usage=usage)]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script), max_turns=20,
                        max_context_chars=10_000_000,
                        registry=_registry("fake_read", lambda **kw: "[ok]"))
    out = a.run("执行一个 20 轮以上的长任务")

    assert "已达最大轮次（20 轮），任务未完成" in out, out
    assert "已完成 20 步" in out, out
    # 收尾是末尾新增的一条 assistant；工具轮里的 assistant 保持原样
    assert a.messages[-1] == {"role": "assistant", "content": out}
    tool_rounds = [m for m in a.messages
                   if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(tool_rounds) == 20, len(tool_rounds)
    assert all(m["content"] == "" for m in tool_rounds)
    # 收尾调用不带 tools，且用量计入本轮统计
    wrapup = a._client.chat.completions.calls[-1]
    assert "tools" not in wrapup, wrapup.keys()
    assert a.usage["llm_calls"] == 21, a.usage
    assert a.usage["total_tokens"] == 12, a.usage
    print("✓ 触顶收尾：20 轮长链另起一条 assistant，旧轮不被改写")


def test_budget_nudge_only_in_last_30_percent():
    """轮次预算提醒：只从第 ceil(0.7 * max_turns) 轮开始出现，且不落盘。"""
    script = _tool_turns(7) + [_Msg(content="收敛完成。")]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script), max_turns=10,
                        max_context_chars=10_000_000,
                        registry=_registry("fake_read", lambda **kw: "[ok]"))
    assert a.run("执行 7 步后收敛") == "收敛完成。"

    calls = a._client.chat.completions.calls
    assert len(calls) == 8, len(calls)
    for i in range(6):        # 第 1～6 轮：末尾没有提醒
        assert calls[i]["messages"][-1]["role"] != "system", calls[i]["messages"][-1]
    tail = calls[6]["messages"][-1]   # 第 7 轮：出现「第 7/10 轮」
    assert tail["role"] == "system" and "第 7/10 轮" in tail["content"], tail
    assert not any("第 7/10 轮" in str(m.get("content")) for m in a.messages), "提醒不该落盘"
    print("✓ 预算提醒：7/10 轮才出现，只进请求体不落盘")


def test_multi_run_long_session_rolls_summary():
    """同一会话连续 6 轮用户输入：滚动摘要后 tool 配对仍完整，轮数受控。"""
    script = []
    for i in range(6):
        script.append(_Msg(tool_calls=[_TC(i * 2 + 1, "fake_read")]))
        script.append(_Msg(content=f"第 {i + 1} 轮完成"))
    a = agent_mod.Agent(verbose=False, client=_StubClient(script), max_turns=3,
                        keep_recent_turns=2,
                        registry=_registry("fake_read", lambda **kw: "[ok] 小结果"))
    a._summarize = lambda text, prev: "已完成的轮次与结论"   # 替身：不发真实请求
    a.max_context_chars = a._context_chars() + 250

    answers = [a.run(f"继续第 {i + 1} 轮") for i in range(6)]
    assert answers == [f"第 {i + 1} 轮完成" for i in range(6)], answers

    assert a.messages[0]["role"] == "system"
    assert a.messages[1].get("_summary") is True, "长会话应插入滚动摘要"
    assert a.messages[1]["content"].startswith(events.SUMMARY_PREFIX)
    users = [m for m in a.messages if m.get("role") == "user"]
    assert len(users) <= 2, f"最近 2 轮之外的历史应被摘要，实际保留 {len(users)} 轮"
    _assert_pairs(a.messages)
    assert a.usage["llm_calls"] == 12, a.usage
    print("✓ 长会话：6 轮后滚动摘要生效，最近两轮原文与工具配对都保留")


def test_cancel_mid_long_chain_stops_early():
    """长链执行到第 6 轮被取消：不再继续跑完剩余轮次，整轮抛 Cancelled。"""
    def _cancel_on_run(**kw):
        tok = current_token()
        assert tok is not None
        tok.cancel("测试中止")
        return "[ok] 已请求取消"

    script = _tool_turns(15, name="fake_cancel") + [_Msg(content="不应执行到这里。")]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script), max_turns=15,
                        max_context_chars=10_000_000,
                        registry=_registry("fake_cancel", _cancel_on_run))
    try:
        a.run("执行一个会被取消的长任务")
    except Cancelled as e:
        reason = str(e)
    else:
        raise AssertionError("长链中途取消应抛出 Cancelled")

    assert reason == "测试中止", reason
    assert a.usage["llm_calls"] <= 7, a.usage      # 取消发生在第 6 轮，不应跑完 15 轮
    assert not any(m.get("_injected") == "max_turns_wrapup" for m in a.messages)
    _assert_pairs(a.messages)
    print("✓ 长链取消：第 6 轮中止，未继续跑剩余轮次")


if __name__ == "__main__":
    print("工作区:", agent_mod.tools.WORKSPACE)
    print("---")
    test_long_chain_30_turns_keeps_all_results()
    test_long_chain_trims_old_tool_results_only()
    test_long_chain_hits_max_turns_with_wrapup()
    test_budget_nudge_only_in_last_30_percent()
    test_multi_run_long_session_rolls_summary()
    test_cancel_mid_long_chain_stops_early()
    print("---")
    print("全部通过 ✅")
