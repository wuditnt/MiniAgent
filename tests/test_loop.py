"""
不花 API 费用地验证 Agent 循环：用一个假的 LLM 客户端。

这是开发 Agent 时最值得写的第一类测试：
把「循环逻辑」和「模型输出」解耦，模型返回什么完全可控。
"""

from __future__ import annotations

import os

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

import agent as agent_mod  # noqa: E402


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
        self.usage = msg.usage      # 缺省 None：与「服务端没回 usage」时一致


class _Scripted:
    """按预设脚本依次返回响应，模拟模型的多轮行为。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = self._msgs.pop(0)
        return _Resp(msg)


class _StubClient:
    def __init__(self, msgs):
        self.chat = type("Chat", (), {"completions": _Scripted(msgs)})()


class _UI:
    """只实现需要的回调 —— UI 是可选能力，_ui_call 会静默跳过缺失的方法。"""

    def __init__(self):
        self.titles = []

    def session_title(self, title):
        self.titles.append(title)


# ---------------------------------------------------------------- 测试

def test_loop_executes_tool_then_answers():
    script = [
        _Msg(tool_calls=[_TC(1, "list_dir", '{"path": "."}')]),   # 第 1 轮：要调工具
        _Msg(content="目录里有这些文件。"),                          # 第 2 轮：给答案
    ]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script))

    out = a.run("看看当前目录")
    assert out == "目录里有这些文件。", out

    roles = [m["role"] for m in a.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"], roles
    assert a.messages[3]["tool_call_id"] == "call_1"
    assert "项" in a.messages[3]["content"] or "file" in a.messages[3]["content"]
    print("✓ 循环：工具调用 → 结果回填 → 最终回答")


def test_bad_args_do_not_break_loop():
    script = [
        _Msg(tool_calls=[_TC(1, "read_file", "{不是合法json")]),   # 参数坏掉
        _Msg(tool_calls=[_TC(2, "no_such_tool", "{}")]),           # 工具不存在
        _Msg(content="已改用其他方式。"),
    ]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script))

    out = a.run("读一个文件")
    assert out == "已改用其他方式。", out
    # 坏参数 → 空 dict → 缺必填 path → 工具内部报错，但循环没崩
    assert "[error]" in a.messages[3]["content"]
    assert "[error]" in a.messages[5]["content"]
    print("✓ 容错：坏参数 / 未知工具都不会中断循环")


def test_context_trim_keeps_recent():
    """上下文超限时，应裁掉「较早」的 tool 结果，而不是最近的。

    注意 protected=6 是硬边界：只有落在最后 6 条之外的 tool 消息才会被裁。
    这里故意把 10 条消息中的 tool 放在 index 2，确保它处于可裁剪区。
    """
    a = agent_mod.Agent(verbose=False, max_context_chars=300)
    a.messages = [
        {"role": "system", "content": "sys" * 10},          # 0 永不裁剪
        {"role": "user", "content": "u1"},                  # 1
        {"role": "tool", "tool_call_id": "c1", "content": "X" * 500},  # 2 应被裁
        {"role": "assistant", "content": "a1"},             # 3
        {"role": "user", "content": "u2"},                  # 4 ┐
        {"role": "assistant", "content": "a2"},             # 5 │
        {"role": "user", "content": "u3"},                  # 6 │ 保护窗口
        {"role": "assistant", "content": "a3"},             # 7 │ (最后 6 条)
        {"role": "user", "content": "u4"},                  # 8 │
        {"role": "assistant", "content": "a4"},             # 9 ┘
    ]
    a._trim_context()
    assert a.messages[2].get("_trimmed") is True, "较早的 tool 结果应被裁剪"
    assert a.messages[0]["content"].startswith("sys"), "system 永不被裁剪"
    assert a.messages[8]["content"] == "u4", "最近的消息应保持原样"
    print("✓ 上下文：超限时裁掉较早的 tool 结果，保留 system 与最近 6 条")


def test_context_summary_rolls_and_keeps_recent():
    """超限时把「较早的整轮」摘要成一段，保留最近 N 轮原文。

    用替身函数顶掉真实模型调用，保证测试不花钱、结果确定。
    """
    a = agent_mod.Agent(verbose=False, max_context_chars=200)
    a.keep_recent_turns = 2
    a.messages = [{"role": "system", "content": "SYS"}]
    for i in range(1, 6):
        a.messages.append({"role": "user", "content": f"u{i}"})
        a.messages.append({"role": "assistant", "content": "y" * 120})
    a._summarize = lambda text, prev: "要点"          # 替身：不发真实请求

    a._trim_context()

    assert a.messages[0]["content"] == "SYS", "system 恒保留"
    assert a.messages[1].get("_summary") is True, "应在 system 后插入历史摘要"
    assert a.messages[1]["content"].startswith("[历史摘要]")
    users = [m["content"] for m in a.messages if m.get("role") == "user"]
    assert users == ["u4", "u5"], f"应只保留最近 2 轮，实际 {users}"
    print("✓ 上下文：超限时滚动摘要，并保留最近 N 轮原文")


def test_context_hard_window_when_summary_fails():
    """摘要失败（网络/模型异常）时，仍要靠滑动窗口硬截断兜住，且不插摘要。"""
    a = agent_mod.Agent(verbose=False, max_context_chars=150)
    a.keep_recent_turns = 2
    a.messages = [{"role": "system", "content": "SYS"}]
    for i in range(1, 6):
        a.messages.append({"role": "user", "content": f"u{i}"})
        a.messages.append({"role": "assistant", "content": "z" * 200})
    a._summarize = lambda text, prev: None            # 替身：模拟摘要失败

    a._trim_context()

    assert a.messages[0]["content"] == "SYS"
    assert not any(m.get("_summary") for m in a.messages), "摘要失败不该插摘要"
    users = [m["content"] for m in a.messages if m.get("role") == "user"]
    assert users == ["u4", "u5"], f"硬截断仍保留最近 2 轮，实际 {users}"
    print("✓ 上下文：摘要失败时滑动窗口硬截断兜底")


def test_context_window_keeps_tool_pairing():
    """硬截断以「轮」为单位，tool 结果不能变成没有对应 tool_calls 的孤儿。"""
    a = agent_mod.Agent(verbose=False, max_context_chars=120)
    a.keep_recent_turns = 1
    a.messages = [{"role": "system", "content": "SYS"}]
    for i in range(1, 4):
        a.messages.append({"role": "user", "content": f"u{i}"})
        a.messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "t", "arguments": "{}"}}],
        })
        a.messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "R" * 100})
    a._summarize = lambda text, prev: None

    a._trim_context()

    ids = {c["id"] for m in a.messages for c in (m.get("tool_calls") or [])}
    for m in a.messages:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in ids, "tool 结果必须有对应的 tool_calls"
    assert len(a.messages) == 4, f"应只剩 system + 最后一轮 3 条，实际 {len(a.messages)}"
    print("✓ 上下文：滑动窗口不会破坏 tool_calls/tool 配对")


def test_max_turns_wrapup_starts_new_bubble():
    """轮数用尽：收尾总结必须另起一条 assistant 消息，不能改写上一轮回答。

    回归点：历史末尾是「content 为空 + tool_calls」的 assistant 与工具结果，
    反向扫描找「最后一条有正文的 assistant」会越过它们，把收尾塞进早已结束的
    上一轮回答里（改旧轮语义、展示串轮）。所以只许追加，不许改写。
    """
    script = [
        _Msg(content="我先看看目录。",
             tool_calls=[_TC(1, "list_dir", '{"path": "."}')]),      # 第 1 轮：正文 + 工具
        _Msg(content="再看一层。",
             tool_calls=[_TC(2, "list_dir", '{"path": "."}')]),      # 第 2 轮：正文 + 工具
        _Msg(content="结论：已完成 A；未完成 B，原因为轮数不够。",       # 收尾轮：总结
             usage=type("U", (), {"prompt_tokens": 3, "completion_tokens": 2,
                                  "total_tokens": 5})()),
    ]
    a = agent_mod.Agent(verbose=False, max_turns=2, client=_StubClient(script))

    out = a.run("做个复杂任务")

    assert "已达最大轮次（2 轮），任务未完成" in out, out
    assert "结论：已完成 A" in out, out
    # 收尾是末尾新增的一条 assistant：旧的两轮回答内容必须原样保留
    assert a.messages[-1] == {"role": "assistant", "content": out}, a.messages[-1]
    earlier = [m["content"] for m in a.messages if m["role"] == "assistant"][:-1]
    assert earlier == ["我先看看目录。", "再看一层。"], earlier
    assert sum(1 for m in a.messages if m["role"] == "assistant") == 3, a.messages
    # 收尾提示只服务于那一次调用，不能留在历史里（否则会被当成用户说过的话）
    assert not any(m.get("_injected") == "max_turns_wrapup" for m in a.messages)
    # 收尾那次调用不带 tools —— 否则还可能再产出 tool_calls 把主循环接回去
    wrapup = a._client.chat.completions.calls[-1]
    assert "tools" not in wrapup, wrapup.keys()
    assert wrapup.get("stream") is False and wrapup.get("timeout") == 60, wrapup
    assert a.usage["llm_calls"] == 3, a.usage
    # 收尾那次调用的用量也计入本轮统计（README 承诺）
    assert a.usage["total_tokens"] == 5, a.usage
    print("✓ 触顶收尾：一次无工具调用生成总结，另起一条 assistant（不改写旧轮）")


def test_max_turns_wrapup_falls_back_when_call_fails():
    """收尾调用本身失败时也要给用户交代：本地合成兜底文案，会话不中断。"""
    script = [
        _Msg(tool_calls=[_TC(1, "no_such_tool", "{}")]),   # 唯一的轮次：调了个不存在的工具
    ]
    a = agent_mod.Agent(verbose=False, max_turns=1, client=_StubClient(script))

    out = a.run("做个复杂任务")   # 收尾脚本已耗尽 → create() 抛错 → 走兜底分支

    assert "已达最大轮次（1 轮），任务未完成" in out, out
    assert "收尾总结未能生成" in out, out
    assert "最后一步是调用 `no_such_tool`" in out, out
    assert "该步报错" in out, out
    assert a.messages[-1]["role"] == "assistant"
    assert not any(m.get("_injected") == "max_turns_wrapup" for m in a.messages)
    print("✓ 触顶收尾：收尾失败时回退本地合成，会话不中断")


def test_extract_hands_model_title_to_ui():
    """记忆提炼那次调用的 JSON 里带 title，应通过 UI 回调交给会话去落地。

    标题搭同一次提炼调用的顺风车，所以这里只断言「解析出来的 title 有没有被
    送出去」；要不要用（用户改过名就丢弃）由 UI 侧决定。
    """
    import tempfile
    from core import settings as settings_mod

    script = [
        _Msg(content='```json\n{"user": [], "project": [], "topic": "", '
                     '"title": "修复上下文泄漏"}\n```'),
    ]
    ui = _UI()
    a = agent_mod.Agent(verbose=False, client=_StubClient(script),
                        ui=ui, session_id="t-title")
    # 提炼门槛受全局配置影响：把配置文件指到空目录，取内置默认（两项均开）
    old_file = settings_mod._SETTINGS_FILE
    settings_mod._SETTINGS_FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
    try:
        a._maybe_extract_memory(
            "请帮我把上下文泄漏的问题查一下并修掉，它已经影响到多轮会话的展示效果了",
            "已定位到 _display_items 的合并口径问题并修复，回归测试全部通过，稍后提交。")
    finally:
        settings_mod._SETTINGS_FILE = old_file
    assert ui.titles == ["修复上下文泄漏"], ui.titles
    print("✓ 提炼：JSON 里的 title 通过 UI 回调交出去（落不落地由会话决定）")


def test_binary_file_is_not_force_decoded():
    import tools
    p = os.path.join(tools.WORKSPACE, "_bin_probe.bin")
    with open(p, "wb") as f:
        f.write(b"%PDF-1.7\x00\x01\x02\xff\xfe" + os.urandom(64))
    try:
        out = tools.call("read_file", {"path": "_bin_probe.bin"})
        assert out.startswith("[binary]"), out[:80]
        assert "run_python" in out
        print("✓ 降级：二进制文件返回 hex 预览并提示改用 run_python")
    finally:
        os.remove(p)


def test_sandbox_blocks_escape():
    import tools
    out = tools.call("read_file", {"path": "../../../../Windows/win.ini"})
    assert "[error]" in out and "越界" in out, out
    print("✓ 沙箱：路径越界被拦截")


def test_dangerous_command_blocked():
    import tools
    out = tools.call("run_shell", {"command": "rm -rf /"})
    assert "[blocked]" in out, out
    print("✓ 安全：危险命令被黑名单拦截")


def test_net_reminder_is_system_role():
    """联网提醒必须是 system 角色，且最后一条 user 必须仍是用户真正说的话。

    回归点：线上出现「你这条消息似乎没有带上内容」——请求末尾曾以 role=user
    追加一条「【系统提示，不是用户发言】…」的联网提醒，模型把它当成用户的最新
    发言，于是回答「你的消息是空的」。
    """
    script = [_Msg(content="答。")]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script))
    a.net_mode = "cloud"
    a.run("问题")

    sent = a._client.chat.completions.calls[0]["messages"]
    assert sent[-1]["role"] == "system", sent[-1]
    assert sent[-1]["content"] == agent_mod.Agent._NET_REMINDER, sent[-1]
    users = [m["content"] for m in sent if m["role"] == "user"]
    assert users == ["问题"], f"最后一条 user 必须是用户的真实输入，实际 {users}"
    print("✓ 联网提醒：以 system 角色追加，不伪造用户消息")


def test_tail_system_rejected_falls_back():
    """服务商只允许第一条 system 时，去掉末尾提醒重发，而不是整轮失败。"""
    msg = _Msg(content="答。")

    class _RejectTail:
        def __init__(self):
            self.calls = []
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kw):
            self.calls.append(kw)
            msgs = kw["messages"]
            if len(msgs) > 1 and msgs[-1].get("role") == "system":
                raise ValueError("system message must be the first message")
            return _Resp(msg)

    client = _RejectTail()
    a = agent_mod.Agent(verbose=False, client=client)
    a.net_mode = "cloud"
    out = a.run("问题")

    assert out == "答。", out
    assert len(client.calls) == 2, client.calls
    assert client.calls[1]["messages"][-1]["role"] == "user", client.calls[1]["messages"][-1]
    print("✓ 联网提醒：服务端拒绝中间 system 时自动去除重发")


def test_empty_answer_retried_once():
    """模型返回空正文且不调工具时，先重试一轮；仍为空则如实返回空，不无限重试。"""
    script = [_Msg(content=""), _Msg(content="补上的答案。")]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script))
    out = a.run("问题")
    assert out == "补上的答案。", out
    assert len(a._client.chat.completions.calls) == 2, a._client.chat.completions.calls
    roles = [m["role"] for m in a.messages]
    assert roles == ["system", "user", "assistant", "assistant"], roles
    print("✓ 空回答：先重试一轮再返回")

    script = [_Msg(content=""), _Msg(content="")]
    b = agent_mod.Agent(verbose=False, client=_StubClient(script))
    out = b.run("问题")
    assert out == "", out
    assert len(b._client.chat.completions.calls) == 2, "只重试一次"
    print("✓ 空回答：只重试一次，不反复空转")


if __name__ == "__main__":
    print("工作区:", agent_mod.tools.WORKSPACE)
    print("---")
    test_loop_executes_tool_then_answers()
    test_bad_args_do_not_break_loop()
    test_context_trim_keeps_recent()
    test_context_summary_rolls_and_keeps_recent()
    test_context_hard_window_when_summary_fails()
    test_context_window_keeps_tool_pairing()
    test_max_turns_wrapup_starts_new_bubble()
    test_max_turns_wrapup_falls_back_when_call_fails()
    test_extract_hands_model_title_to_ui()
    test_binary_file_is_not_force_decoded()
    test_sandbox_blocks_escape()
    test_dangerous_command_blocked()
    test_net_reminder_is_system_role()
    test_tail_system_rejected_falls_back()
    test_empty_answer_retried_once()
    print("---")
    print("全部通过 ✅")
