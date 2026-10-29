"""
内联提问（ask_user）测试 —— 免费用例：纯函数 + 假 asker，不碰网络、不烧 token。

覆盖四件事：
  · ask_user 的返回协议：无通道 / 有答案 / 未作答 / 通道故障，四种都不抛异常；
  · 工具与注册表契约：exclusive（必须串行，避免并发弹多张卡片）、幂等注册；
  · 等待桥的唤醒语义：_Session.answer_ask 按 cid 精确匹配，不匹配不动别人的等待；
  · 关闭 UI（enabled=False）时危险确认不提问、不阻塞 stdin，直接按默认值否决。
"""

from __future__ import annotations

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import ask  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402
from core.webui import _Session  # noqa: E402


def _with_asker(fn):
    """临时注入 asker 并保证用完摘除（模块级状态，别污染其它用例）。"""
    ask.set_asker(fn)
    try:
        return ask.ask_user("选哪个？", ["A", "B"], True)
    finally:
        ask.set_asker(None)


# ---------------------------------------------------------------- 返回协议

def test_no_channel():
    ask.set_asker(None)
    out = ask.ask_user("选哪个？", ["A", "B"])
    assert out.startswith(ask.UNANSWERED)
    assert "没有人工问答通道" in out


def test_answered():
    assert _with_asker(lambda q, o, f, m: "B") == "[answer] B"


def test_answer_is_stripped():
    assert _with_asker(lambda q, o, f, m: "  B  ") == "[answer] B"


def test_blank_answer_is_unanswered():
    out = _with_asker(lambda q, o, f, m: "")
    assert out.startswith(ask.UNANSWERED)
    assert "用户未作答" in out


def test_asker_failure_is_unanswered():
    def boom(_q, _o, _f, _m):
        raise RuntimeError("桥炸了")

    out = _with_asker(boom)
    assert out.startswith(ask.UNANSWERED)
    assert "通道故障" in out


def test_asker_receives_normalized_args():
    seen = {}

    def rec(q, o, f, m):
        seen.update(question=q, options=o, allow_free_text=f, multi=m)
        return "ok"

    _with_asker(rec)
    assert seen["question"] == "选哪个？"
    assert seen["options"] == ["A", "B"]
    assert seen["allow_free_text"] is True
    assert seen["multi"] is False


def test_blank_question_falls_back():
    seen = {}
    ask.set_asker(lambda q, o, f, m: seen.update(question=q) or "ok")
    try:
        ask.ask_user("   ", [])
    finally:
        ask.set_asker(None)
    assert seen["question"] == "(未提供问题)"


def test_multi_passed_through():
    seen = {}

    def rec(q, o, f, m):
        seen.update(multi=m)
        return "A、B"

    ask.set_asker(rec)
    try:
        out = ask.ask_user("选哪些？", ["A", "B", "C"], True, multi=True)
    finally:
        ask.set_asker(None)
    assert seen["multi"] is True
    assert out == "[answer] A、B"


# ---------------------------------------------------------------- 选项规范化

def test_options_clean():
    assert ask._clean_options([" a ", "", "a", "b"]) == ["a", "b"]
    assert ask._clean_options(None) == []
    assert ask._clean_options("单值") == ["单值"]
    assert ask._clean_options(["x"] * 3) == ["x"]


def test_options_capped():
    raw = [f"o{i}" for i in range(20)]
    assert len(ask._clean_options(raw)) == ask.MAX_OPTIONS


# ---------------------------------------------------------------- 工具契约

def test_tool_flags():
    t = ask._ASK_USER_TOOL
    assert t.name == "ask_user"
    assert t.exclusive is True          # 必须串行：同批不能并发弹多张卡片
    assert t.dangerous is False
    assert t.module == "core.ask"
    assert t.parameters["required"] == ["question"]
    assert "multi" in t.parameters["properties"]


def test_register_is_idempotent():
    reg = ToolRegistry()
    ask.register(reg)
    ask.register(reg)
    assert [t.name for t in reg.all()].count("ask_user") == 1


# ---------------------------------------------------------------- 等待桥

def test_answer_ask_matches_cid():
    session = _Session("t", None)
    import threading

    event = threading.Event()
    holder = {"answer": ""}
    session.pending_ask = ("abc123", event, holder)

    assert session.answer_ask("wrong", "nope") is False
    assert not event.is_set()

    assert session.answer_ask("abc123", "选 A") is True
    assert event.is_set()
    assert holder["answer"] == "选 A"
    assert session.pending_ask[0] == "abc123"


def test_answer_ask_without_pending():
    session = _Session("t", None)
    assert session.answer_ask("abc123", "x") is False


def test_deny_pending_ask_wakes_with_blank():
    session = _Session("t", None)
    import threading

    event = threading.Event()
    holder = {"answer": "未清的旧值"}
    session.pending_ask = ("cid", event, holder)

    session.deny_pending_ask()
    assert event.is_set()
    assert holder["answer"] == ""       # 空答案 = 未作答，交给 agent 按未回答继续


# ---------------------------------------------------------------- 关闭 UI 时不得交互

class _DangerTool:
    name = "run_shell"


def test_ask_confirm_disabled_does_not_prompt():
    """enabled=False（管道 / 后台）：不提问、不阻塞 stdin，直接按默认值（否决）返回。"""
    import builtins

    from core.ui import PlainUI, RichUI

    def _no_input(*_a, **_k):
        raise AssertionError("关闭 UI 时不该读 stdin")

    class _NoConsole:
        def print(self, *_a, **_k):
            raise AssertionError("关闭 UI 时不该输出")

        def input(self, *_a, **_k):
            raise AssertionError("关闭 UI 时不该提问")

    real = builtins.input
    builtins.input = _no_input
    try:
        args = {"command": "rm -rf /"}
        assert PlainUI(enabled=False).ask_confirm(_DangerTool(), args) is False
        assert RichUI(enabled=False, console=_NoConsole()).ask_confirm(_DangerTool(), args) is False
    finally:
        builtins.input = real
    print("✓ 关闭 UI 时危险工具确认：不提问、不阻塞，默认否决")


if __name__ == "__main__":
    for _t in (
        test_no_channel, test_answered, test_answer_is_stripped,
        test_blank_answer_is_unanswered, test_asker_failure_is_unanswered,
        test_asker_receives_normalized_args, test_blank_question_falls_back,
        test_multi_passed_through, test_options_clean, test_options_capped,
        test_tool_flags, test_register_is_idempotent, test_answer_ask_matches_cid,
        test_answer_ask_without_pending, test_deny_pending_ask_wakes_with_blank,
        test_ask_confirm_disabled_does_not_prompt,
    ):
        _t()
    print("✓ 内联提问 16 项通过")
