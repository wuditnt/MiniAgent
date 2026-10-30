"""
工具检索（standard 档位的按需加载）：不花 API 费用，用假客户端验证整条链路。

覆盖点：
  · 低频分组默认不常驻，search_tools 能按中文关键词把它们加载进「当前会话」；
  · 没命中时给出可检索清单，而不是一句「没有」；
  · 加载后的工具在**同一轮 run 的下一个回合**就出现在请求的 tools 里；
  · full 档不加载 search_tools（所有工具都已声明，检索没有意义）；
  · 前缀缓存命中数能进 usage（缓存生效的唯一证据）；
  · 工具表逐字节稳定（前缀缓存能命中的前提）。
"""

from __future__ import annotations

import json

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

import agent as agent_mod  # noqa: E402
from core import registry as registry_mod  # noqa: E402
from core import tool_search  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402
from core.webui import _session_registry  # noqa: E402


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


# ---------------------------------------------------------------- 测试

def test_standard_profile_keeps_common_tools_only():
    """standard：常用工具常驻 + search_tools 在场；低频分组与全量档各就各位。"""
    std = _session_registry("agent", "ask", False, profile="standard")
    full = _session_registry("agent", "ask", False, profile="full")

    assert std.get("search_tools") is not None, "standard 缺少 search_tools，模型无从发现低频能力"
    assert std.get("zip_create") is None, "archive 组应被裁掉"
    assert std.get("read_file") is not None, "常用工具不能被裁"
    # 全量档：所有工具都已声明，检索只会有害无益
    assert full.get("search_tools") is None, "full 档不应加载 search_tools"
    assert full.get("zip_create") is not None
    print("✓ 档位：standard=常驻+检索，full=全量且不带检索工具")


def test_search_loads_tools_into_current_registry():
    """中文关键词能命中工具并加载进当前会话表（不动全局 REGISTRY）。"""
    reg = ToolRegistry()
    tool_search.register(reg)
    before_global = set(registry_mod.REGISTRY.names())

    tok = registry_mod.set_current_registry(reg)
    try:
        out = tool_search.search_tools("帮我解压这个 zip 压缩包")
    finally:
        registry_mod.reset_current_registry(tok)

    assert "[ok]" in out and "已加载" in out, out
    assert reg.get("zip_create") is not None or reg.get("zip_extract") is not None, out
    assert set(registry_mod.REGISTRY.names()) == before_global, "检索不能写全局工具表"
    print("✓ 检索：中文关键词命中并加载到本会话")


def test_search_without_match_lists_domains():
    """没命中时给出「能搜什么」的清单，避免模型直接回答做不到。"""
    reg = ToolRegistry()
    tool_search.register(reg)
    tok = registry_mod.set_current_registry(reg)
    try:
        out = tool_search.search_tools("zzz_完全不存在的能力_zzz")
    finally:
        registry_mod.reset_current_registry(tok)

    assert "可检索" in out, out
    assert "OCR" in out and "压缩包" in out, out
    assert len(reg) == 1, "没命中就不该加载任何工具（只有 search_tools 本身）"
    print("✓ 检索：无命中时返回可检索清单")


def test_loaded_tool_visible_in_next_turn():
    """search_tools 的加载结果必须出现在同一轮 run 的下一次请求里。"""
    reg = _session_registry("agent", "ask", False, profile="standard")
    script = [
        _Msg(tool_calls=[_TC(1, "search_tools", '{"query": "解压 zip"}')]),
        _Msg(content="已经具备解压能力。"),
    ]
    a = agent_mod.Agent(verbose=False, client=_StubClient(script), registry=reg)
    out = a.run("帮我解压这个包")

    assert out == "已经具备解压能力。", out
    calls = a._client.chat.completions.calls
    first = {s["function"]["name"] for s in calls[0]["tools"]}
    second = {s["function"]["name"] for s in calls[1]["tools"]}
    assert "zip_create" not in first, "第一轮就不该有 archive 工具"
    assert {"zip_create", "zip_extract"} & second, f"第二轮仍未声明：{sorted(second)[:10]}"
    print("✓ 循环：检索加载的工具在下一回合即可见")


def test_cache_hit_tokens_accounted():
    """用量里要能读到前缀缓存命中数（服务商字段名不同，统一成一个）。"""
    class _Usage:
        prompt_tokens = 1000
        completion_tokens = 20
        total_tokens = 1020
        prompt_cache_hit_tokens = 800

    a = agent_mod.Agent(verbose=False, client=_StubClient([_Msg(content="答。", usage=_Usage())]))
    a.run("问")
    assert a.usage["cached_tokens"] == 800, a.usage
    assert a.usage["prompt_tokens"] == 1000, a.usage
    print("✓ 缓存：命中数计入 usage")


def test_tool_schema_bytes_stable():
    """同一份工具表两次运行产出的 tools 必须逐字节一致，否则前缀缓存永远不命中。"""
    a = agent_mod.Agent(
        verbose=False,
        client=_StubClient([_Msg(content="一")]),
        registry=_session_registry("agent", "ask", False, profile="standard"),
    )
    b = agent_mod.Agent(
        verbose=False,
        client=_StubClient([_Msg(content="二")]),
        registry=_session_registry("agent", "ask", False, profile="standard"),
    )
    a.run("x")
    b.run("y")
    left = json.dumps(a._client.chat.completions.calls[0]["tools"], ensure_ascii=False)
    right = json.dumps(b._client.chat.completions.calls[0]["tools"], ensure_ascii=False)
    assert left == right, "工具表顺序不稳定：前缀缓存会整片失效"
    print("✓ 缓存：工具表逐字节稳定")


if __name__ == "__main__":
    test_standard_profile_keeps_common_tools_only()
    test_search_loads_tools_into_current_registry()
    test_search_without_match_lists_domains()
    test_loaded_tool_visible_in_next_turn()
    test_cache_hit_tokens_accounted()
    test_tool_schema_bytes_stable()
    print("---")
    print("全部通过 ✅")
