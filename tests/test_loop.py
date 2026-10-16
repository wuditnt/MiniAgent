"""
不花 API 费用地验证 Agent 循环：用一个假的 LLM 客户端。

这是开发 Agent 时最值得写的第一类测试：
把「循环逻辑」和「模型输出」解耦，模型返回什么完全可控。
"""

from __future__ import annotations

import os

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

os.environ.setdefault("OPENAI_API_KEY", "test-key")

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
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})]


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


if __name__ == "__main__":
    print("工作区:", agent_mod.tools.WORKSPACE)
    print("---")
    test_loop_executes_tool_then_answers()
    test_bad_args_do_not_break_loop()
    test_context_trim_keeps_recent()
    test_context_summary_rolls_and_keeps_recent()
    test_context_hard_window_when_summary_fails()
    test_context_window_keeps_tool_pairing()
    test_binary_file_is_not_force_decoded()
    test_sandbox_blocks_escape()
    test_dangerous_command_blocked()
    print("---")
    print("全部通过 ✅")
