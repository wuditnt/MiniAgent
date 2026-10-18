"""
事件流测试 —— 免费用例（假客户端 / 纯函数，不碰网络、不烧 token）。

覆盖 P0 的硬骨架：
  · 只追加 + seq 单调（文件永不重写）；
  · 请求可重建（I1）：仅用事件流重放即还原 llm_request 当时发出的 messages；
  · 裁剪留痕（context_inject 三类）、工具与审批事件、取消事件；
  · 超限值显式截断、关闭开关时零副作用（回归保护）。
"""

from __future__ import annotations

import json
import os
import uuid

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import agent as agent_mod  # noqa: E402
from core import events  # noqa: E402
from core.cancel import Cancelled  # noqa: E402
from core.registry import ToolRegistry, tool  # noqa: E402


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
    def __init__(self, msgs):
        self._msgs = list(msgs)

    def create(self, **kwargs):
        return _Resp(self._msgs.pop(0))


class _StubClient:
    def __init__(self, msgs):
        self.chat = type("Chat", (), {"completions": _Scripted(msgs)})()


def _two_turn_script():
    """两轮：先要一个工具，再给答案（够触发 tool_call/tool_result/两次 llm_request）。"""
    return [
        _Msg(tool_calls=[_TC(1, "list_dir", '{"path": "."}')]),
        _Msg(content="目录里有这些文件。"),
    ]


def _events_of(sid: str, etype: str) -> list[dict]:
    return [e for e in events.iter_events(sid) if e.get("type") == etype]


def _assert_all_requests_rebuild(sid: str) -> int:
    """对事件流里每条 llm_request 调 rebuild_messages 并逐条比对 sha256。"""
    reqs = _events_of(sid, events.LLM_REQUEST)
    assert reqs, "没有 llm_request 事件"
    for ev in reqs:
        msgs = events.rebuild_messages(sid, ev["seq"])
        digests = ev["payload"]["messages"]
        assert len(msgs) == len(digests), (ev["seq"], len(msgs), len(digests))
        for m, d in zip(msgs, digests):
            assert events.message_digest(m)["sha256"] == d["sha256"], (ev["seq"], m)
    return len(reqs)


def _big_registry(size: int = 600) -> ToolRegistry:
    """一个大输出工具：用来把上下文迅速撑到超限。"""
    reg = ToolRegistry()

    @tool("big_out", "返回一段固定长度的大文本（测试用）。", registry=reg)
    def big_out(tag: str = "") -> str:
        return ("R" * size) + (tag or "")

    return reg


def _bulky_history(turns: int, tool_chars: int) -> list[dict]:
    """system + N 轮（user / assistant(tool_calls) / 大体积 tool 结果）。"""
    msgs: list[dict] = [{"role": "system", "content": "SYS"}]
    for i in range(1, turns + 1):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "t", "arguments": "{}"}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "t",
                     "content": "R" * tool_chars})
    return msgs


# ---------------------------------------------------------------- 工具

def _new_sid() -> str:
    return "_t_events_" + uuid.uuid4().hex[:8]


def _cleanup(sid: str) -> None:
    try:
        os.remove(events.events_path(sid))
    except OSError:
        pass


def _lines(sid: str) -> list[dict]:
    with open(events.events_path(sid), "r", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# ---------------------------------------------------------------- 用例 1

def test_append_only_and_seq():
    """多次 emit 行数增长、seq 严格递增；重开 writer 续写不覆盖、不重号。"""
    sid = _new_sid()
    try:
        w = events.EventWriter(sid)
        w.set_run("r-1")
        for i in range(3):
            w.emit(events.USER_MESSAGE, source=events.SRC_USER, phase=events.PH_RUN_START,
                   content=f"msg{i}")
        w.close()

        evs = _lines(sid)
        assert len(evs) == 3, evs
        assert [e["seq"] for e in evs] == [1, 2, 3], [e["seq"] for e in evs]
        assert all(e["v"] == events.VERSION for e in evs)
        assert all(e["run_id"] == "r-1" for e in evs)

        # 重开：续写而非覆盖，seq 从 4 起
        w2 = events.EventWriter(sid)
        assert events.last_seq(sid) == 3
        w2.emit(events.RUN_END, source=events.SRC_LOOP, phase=events.PH_RUN_END,
                status="done")
        w2.close()

        evs = _lines(sid)
        assert len(evs) == 4, "重开 writer 不应覆盖已有行"
        assert [e["seq"] for e in evs] == [1, 2, 3, 4]
        print("✓ 事件流：只追加、seq 严格递增、重开续写不重号")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 用例 7

def test_truncation_marked():
    """超 value_limit 的字符串被显式标记截断，len / sha256 / head / tail 正确。"""
    sid = _new_sid()
    try:
        limit = 4096
        w = events.EventWriter(sid, value_limit=limit)
        big = "字" * (limit + 500)
        w.emit(events.SYSTEM_PROMPT, source=events.SRC_SYSTEM, phase=events.PH_RUN_START,
               content=big)
        w.close()

        payload = _lines(sid)[0]["payload"]
        mark = payload["content"]
        assert mark.get("__truncated__") is True, mark
        assert mark["len"] == len(big)
        assert mark["sha256"] == events.sha256_text(big)
        assert mark["head"] == big[:2048]
        assert mark["tail"] == big[-2048:]
        print("✓ 事件流：超限字符串被显式标记截断（len/sha256/head/tail 均可校验）")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 纯函数重建（I1 的骨架）

def test_rebuild_manual_stream():
    """手工构造事件流，验证 rebuild_messages 的重放规则（system/用户/响应/工具/裁剪）。"""
    sid = _new_sid()
    try:
        w = events.EventWriter(sid)
        w.set_run("r-1")
        sys_text = "你是智能体"
        w.emit(events.SYSTEM_PROMPT, source=events.SRC_SYSTEM, phase=events.PH_RUN_START,
               content=sys_text, chars=len(sys_text), sha256=events.sha256_text(sys_text))
        w.emit(events.USER_MESSAGE, source=events.SRC_USER, phase=events.PH_RUN_START,
               content="看看目录")

        # 第 1 轮：只带 system + user
        msgs1 = [{"role": "system", "content": sys_text},
                 {"role": "user", "content": "看看目录"}]
        req1 = w.emit(events.LLM_REQUEST, source=events.SRC_MODEL, phase=events.PH_TURN, turn=1,
                      model="stub", temperature=0.0, tools=["list_dir"],
                      message_count=len(msgs1), chars=0,
                      system_sha256=events.sha256_text(sys_text),
                      messages=[events.message_digest(m) for m in msgs1])
        assert events.rebuild_messages(sid, req1["seq"]) == msgs1

        # 模型要调工具 → 结果回填
        tcs = [{"id": "call_1", "type": "function",
                "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]
        w.emit(events.LLM_RESPONSE, source=events.SRC_MODEL, phase=events.PH_TURN, turn=1,
               sec=0.1, streamed=False, content="", tool_calls=tcs)
        w.emit(events.TOOL_RESULT, source=events.SRC_TOOL, phase=events.PH_TURN, turn=1,
               id="call_1", name="list_dir", ok=True, sec=0.01, content="a.txt\nb.txt")

        # 第 2 轮：带 assistant(tool_calls) + tool 结果
        msgs2 = msgs1 + [
            {"role": "assistant", "content": "", "tool_calls": tcs},
            {"role": "tool", "tool_call_id": "call_1", "name": "list_dir",
             "content": "a.txt\nb.txt"},
        ]
        req2 = w.emit(events.LLM_REQUEST, source=events.SRC_MODEL, phase=events.PH_TURN, turn=2,
                      model="stub", temperature=0.0, tools=["list_dir"],
                      message_count=len(msgs2), chars=0,
                      system_sha256=events.sha256_text(sys_text),
                      messages=[events.message_digest(m) for m in msgs2])
        assert events.rebuild_messages(sid, req2["seq"]) == msgs2

        # 第一级裁剪：老工具结果被换成占位符 → 第 3 轮请求跟着变
        w.emit(events.CONTEXT_INJECT, source=events.SRC_LOOP, phase=events.PH_CONTEXT,
               kind=events.KIND_TRIM_TOOL_RESULTS, count=1, ids=["call_1"])
        msgs3 = msgs2[:-1] + [{"role": "tool", "tool_call_id": "call_1", "name": "list_dir",
                               "content": events.TRIMMED_PLACEHOLDER}]
        req3 = w.emit(events.LLM_REQUEST, source=events.SRC_MODEL, phase=events.PH_TURN, turn=3,
                      model="stub", temperature=0.0, tools=["list_dir"],
                      message_count=len(msgs3), chars=0,
                      system_sha256=events.sha256_text(sys_text),
                      messages=[events.message_digest(m) for m in msgs3])
        assert events.rebuild_messages(sid, req3["seq"]) == msgs3
        w.close()
        print("✓ 事件流：手工流重放可还原每一轮 llm_request（含裁剪）")
    finally:
        _cleanup(sid)


def test_rebuild_detects_corruption():
    """重放结果与请求摘要不一致时，rebuild 必须报错而不是给出错误答案。"""
    sid = _new_sid()
    try:
        w = events.EventWriter(sid)
        sys_text = "SYS"
        w.emit(events.SYSTEM_PROMPT, source=events.SRC_SYSTEM, phase=events.PH_RUN_START,
               content=sys_text, sha256=events.sha256_text(sys_text))
        real = [{"role": "system", "content": sys_text}, {"role": "user", "content": "hi"}]
        w.emit(events.USER_MESSAGE, source=events.SRC_USER, phase=events.PH_RUN_START, content="hi")
        # 故意把第 2 条消息的摘要写成别的文本的哈希
        bad = [events.message_digest(real[0]),
               {"role": "user", "sha256": events.sha256_text("别的"), "chars": 2}]
        req = w.emit(events.LLM_REQUEST, source=events.SRC_MODEL, phase=events.PH_TURN, turn=1,
                     model="stub", temperature=0.0, tools=[], message_count=2, chars=0,
                     system_sha256=events.sha256_text(sys_text), messages=bad)
        w.close()
        try:
            events.rebuild_messages(sid, req["seq"])
        except AssertionError as e:
            assert "I1 破坏" in str(e)
            print("✓ 事件流：重放与请求摘要不一致时立即报错（不变式有牙齿）")
            return
        raise AssertionError("摘要不一致时 rebuild 竟然没报错")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 用例 2（P0 核心）

def test_rebuild_invariant():
    """两轮含工具的真实循环跑完后，每条 llm_request 都能仅用事件流重建并校验通过。"""
    sid = _new_sid()
    try:
        a = agent_mod.Agent(verbose=False, client=_StubClient(_two_turn_script()),
                            system_prompt="SYS")
        w = events.EventWriter(sid)
        w.set_run("r-1")
        a.event_writer = w
        out = a.run("看看当前目录")
        w.close()

        assert out == "目录里有这些文件。", out
        n = _assert_all_requests_rebuild(sid)
        assert n >= 2, f"两轮至少两次 llm_request，实际 {n}"
        # 事件流里必须同时有用户消息、工具调用与工具结果
        for etype in (events.USER_MESSAGE, events.TOOL_CALL, events.TOOL_RESULT):
            assert _events_of(sid, etype), f"缺少 {etype} 事件"
        print("✓ 事件流：两轮含工具的运行，每条 llm_request 均可重放还原（I1）")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 用例 3

def test_context_inject_recorded():
    """三级收敛各自留痕（trim_tool_results / summary / hard_window），裁剪后仍可重建。"""
    sid_a = _new_sid()
    try:
        a = agent_mod.Agent(verbose=False, system_prompt="SYS", max_context_chars=200)
        a.keep_recent_turns = 2
        w = events.EventWriter(sid_a)
        a.event_writer = w
        # 第一次：摘要成功 → trim_tool_results + summary
        a.messages = _bulky_history(6, 300)
        a._summarize = lambda text, prev: "要点"
        a._trim_context()
        # 第二次：摘要失败 → trim_tool_results + hard_window 兜底
        a.messages = _bulky_history(6, 300)
        a._summarize = lambda text, prev: None
        a._trim_context()
        w.close()

        kinds = {e["payload"].get("kind") for e in _events_of(sid_a, events.CONTEXT_INJECT)}
        want = {events.KIND_TRIM_TOOL_RESULTS, events.KIND_SUMMARY, events.KIND_HARD_WINDOW}
        assert want <= kinds, f"缺少裁剪事件：{want - kinds}"
        summary = [e for e in _events_of(sid_a, events.CONTEXT_INJECT)
                   if e["payload"].get("kind") == events.KIND_SUMMARY][0]
        assert summary["payload"]["content"] == "要点"
        assert summary["payload"]["boundary"] > 0
    finally:
        _cleanup(sid_a)

    # 真实运行：上下文超限触发裁剪，之后每条请求仍然可重建
    sid_b = _new_sid()
    try:
        script = [_Msg(tool_calls=[_TC(i, "big_out", "{}")]) for i in range(1, 7)]
        script.append(_Msg(content="结束"))
        a2 = agent_mod.Agent(verbose=False, client=_StubClient(script),
                             registry=_big_registry(600), system_prompt="SYS",
                             max_context_chars=1500, keep_recent_turns=2)
        a2._summarize = lambda text, prev: "要点"
        w2 = events.EventWriter(sid_b)
        w2.set_run("r-2")
        a2.event_writer = w2
        a2.run("跑六轮")
        w2.close()

        # 单次 run 只有一个 user 消息（轮数以 user 消息为界），所以第二/三级
        # （summary / hard_window）在真实运行里结构上不可能触发 —— 它们已由上面
        # 手工调用 _trim_context 的两段取证覆盖。这里只验证真实运行会留下的
        # 一级裁剪，以及裁剪之后每条请求仍可重建。
        kinds = {e["payload"].get("kind") for e in _events_of(sid_b, events.CONTEXT_INJECT)}
        assert events.KIND_TRIM_TOOL_RESULTS in kinds, f"六轮大输出应触发一级裁剪，实际 {kinds}"
        _assert_all_requests_rebuild(sid_b)
        print("✓ 事件流：真实运行超限触发裁剪留痕，且裁剪之后每条请求仍可重建")
    finally:
        _cleanup(sid_b)


# ---------------------------------------------------------------- 用例 4

def test_tool_and_approval_events():
    """ask 档 + 危险工具 + 用户拒绝 → tool_call → approval(deny) → tool_result(ok=false)。"""
    sid = _new_sid()
    try:
        class _DenyUI:
            def ask_confirm(self, tool, args):
                return False

        script = [_Msg(tool_calls=[_TC(1, "run_shell", '{"command": "echo hi"}')]),
                  _Msg(content="好的，我换一种方式。")]
        a = agent_mod.Agent(verbose=False, client=_StubClient(script),
                            ui=_DenyUI(), approve=True, system_prompt="SYS")
        w = events.EventWriter(sid)
        w.set_run("r-1")
        a.event_writer = w
        a.run("跑个命令")
        w.close()

        calls = _events_of(sid, events.TOOL_CALL)
        approvals = _events_of(sid, events.APPROVAL)
        results = _events_of(sid, events.TOOL_RESULT)
        assert calls and approvals and results, (len(calls), len(approvals), len(results))
        assert calls[0]["payload"]["name"] == "run_shell"
        assert calls[0]["payload"]["dangerous"] is True
        assert calls[0]["payload"]["confirm"] == "ask", calls[0]["payload"]
        assert approvals[0]["payload"]["decision"] == "deny", approvals[0]["payload"]
        assert approvals[0]["payload"]["mode"] == "ask"
        assert results[0]["payload"]["ok"] is False
        assert results[0]["payload"]["content"].startswith("[denied]")
        # 顺序：tool_call 先于 approval 先于 tool_result
        types = [e["type"] for e in events.iter_events(sid)]
        assert types.index(events.TOOL_CALL) < types.index(events.APPROVAL)
        assert types.index(events.APPROVAL) < types.index(events.TOOL_RESULT)
        print("✓ 事件流：危险工具被拒 → tool_call / approval(deny) / tool_result(ok=false)")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 用例 5

def test_cancel_event():
    """取消 → cancel + run_end(status=cancelled)，且消息补齐行为与之前一致。"""
    sid = _new_sid()
    try:
        class _CancelUI:
            def __init__(self):
                self.agent = None

            def llm_done(self, turn, sec, streamed=False):
                self.agent.interrupt("测试取消")   # 模型刚回完就取消

        ui = _CancelUI()
        script = [_Msg(tool_calls=[_TC(1, "list_dir", '{"path": "."}')]),
                  _Msg(content="不该走到这一步")]
        a = agent_mod.Agent(verbose=False, client=_StubClient(script), ui=ui,
                            system_prompt="SYS")
        ui.agent = a
        w = events.EventWriter(sid)
        w.set_run("r-1")
        a.event_writer = w
        try:
            a.run("看看目录")
            raise AssertionError("应该抛 Cancelled")
        except Cancelled:
            pass
        finally:
            w.close()

        assert _events_of(sid, events.CANCEL), "缺少 cancel 事件"
        runs = _events_of(sid, events.RUN_END)
        assert runs and runs[-1]["payload"]["status"] == "cancelled", runs
        # 消息补齐：取消的工具回复被替换成统一文案（循环行为不变）
        assert any(
            m.get("role") == "tool" and m.get("content") == agent_mod.CANCELLED_TEXT
            for m in a.messages
        ), a.messages
        print("✓ 事件流：取消留痕（cancel + run_end=cancelled），消息补齐行为不变")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 用例 6

def test_events_disabled_noop():
    """events_enabled=false → 不生成文件，且 messages 与开启时逐条相等（回归保护）。"""
    sid_on, sid_off = _new_sid(), _new_sid()
    try:
        a_on = agent_mod.Agent(verbose=False, client=_StubClient(_two_turn_script()),
                               system_prompt="SYS")
        w = events.EventWriter(sid_on)
        a_on.event_writer = w
        out_on = a_on.run("看看当前目录")
        w.close()

        a_off = agent_mod.Agent(verbose=False, client=_StubClient(_two_turn_script()),
                                system_prompt="SYS")
        w2 = events.EventWriter(sid_off, enabled=False)   # 完全 no-op
        a_off.event_writer = w2
        out_off = a_off.run("看看当前目录")
        w2.close()

        assert out_on == out_off
        assert a_on.messages == a_off.messages, "开关只影响记录，绝不该改变消息序列"
        assert events.events_exist(sid_on)
        assert not events.events_exist(sid_off), "关闭事件流不该产生任何文件"
        print("✓ 事件流：关闭开关 = 纯 no-op（不建文件、messages 逐条相同）")
    finally:
        _cleanup(sid_on)
        _cleanup(sid_off)


# ---------------------------------------------------------------- 用例 8

def test_rebuild_display_fallback():
    """会话 JSON 丢失时，rebuild_display 能从事件流恢复出等价的展示项。"""
    sid = _new_sid()
    try:
        a = agent_mod.Agent(verbose=False, client=_StubClient(_two_turn_script()),
                            system_prompt="SYS")
        w = events.EventWriter(sid)
        w.set_run("r-1")
        a.event_writer = w
        a.run("看看当前目录")
        w.close()

        # 模拟 webui._Session.save 落盘的展示态（只留 user / assistant，去私有字段）
        json_msgs = [{k: v for k, v in m.items() if not k.startswith("_")}
                     for m in a.messages[1:] if m.get("role") in ("user", "assistant")]
        from core import webui

        expected = webui._display_items(json_msgs)
        actual = events.rebuild_display(sid)

        def core_fields(items):
            # _end 是「在 raw 列表中的下标」，两条链路的 raw 粒度不同（一个含 tool、一个不含），
            # 因此只比对展示语义（role / content / attachments）。
            return [{k: v for k, v in it.items() if k != "_end"} for it in items]

        assert core_fields(actual) == core_fields(expected), (actual, expected)
        print("✓ 事件流：JSON 丢失时可用事件流恢复等价的展示项")
    finally:
        _cleanup(sid)


# ---------------------------------------------------------------- 入口

if __name__ == "__main__":
    test_append_only_and_seq()
    test_rebuild_invariant()
    test_context_inject_recorded()
    test_tool_and_approval_events()
    test_cancel_event()
    test_events_disabled_noop()
    test_truncation_marked()
    test_rebuild_manual_stream()
    test_rebuild_detects_corruption()
    test_rebuild_display_fallback()
    print("---")
    print("全部通过 ✅")
