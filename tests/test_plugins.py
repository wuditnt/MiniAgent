"""
插件层与 MCP 层的测试。不需要 API Key，不花任何费用。

覆盖四类容易出错的地方：
  1. Schema 自动推导 —— 类型映射、必填判定、docstring 抽取、Optional 处理
  2. 注册表语义     —— 冲突检测、按模块热删除、分组/来源检索
  3. 插件加载器     —— 发现、热重载、坏插件隔离
  4. MCP 通道       —— 命名合规、真实握手、调用、错误上报
"""

from __future__ import annotations

import json
import os
import shutil
import sys

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

from core.registry import ToolRegistry, tool  # noqa: E402
from core.schema import schema_from_callable  # noqa: E402
from loader import PluginLoader  # noqa: E402


# ============================================================ 1. Schema 推导

def test_schema_from_annotations():
    def sample(path: str, count: int = 3, ratio: float = 0.5, flag: bool = False,
               tags: list | None = None, note: "str" = "") -> str:
        """做点什么。

        Args:
            path: 输入路径
            count: 次数
        """
        return ""

    s = schema_from_callable(sample)
    props = s["properties"]
    assert props["path"]["type"] == "string", props
    assert props["count"] == {"type": "integer", "description": "次数", "default": 3}, props["count"]
    assert props["ratio"]["type"] == "number"
    assert props["flag"]["type"] == "boolean"
    assert props["tags"]["type"] == "array", props["tags"]
    assert props["note"]["type"] == "string", props["note"]          # 字符串注解
    assert props["path"]["description"] == "输入路径"                  # docstring 抽取
    assert s["required"] == ["path"], s["required"]                  # 只有无默认值的才算必填
    print("✓ Schema：类型映射 / 必填判定 / docstring 抽取 / 字符串注解 全部正确")


def test_schema_optional_and_sphinx():
    def f(a: str | None = None, b: int = 1) -> str:
        """:param a: 可空参数
        :param b: sphinx 描述
        """
        return ""

    s = schema_from_callable(f)
    assert s["properties"]["a"]["type"] == "string", s
    assert s["properties"]["a"]["description"] == "可空参数", s           # Optional[X] 与 | None
    assert s["properties"]["b"]["description"] == "sphinx 描述", s      # Sphinx 风格
    assert s["required"] == [], s
    print("✓ Schema：Optional[X] 与 X|None 归一化、Sphinx 风格 :param: 识别正确")


def test_manual_params_override():
    reg = ToolRegistry()

    @tool("mix", "混合写法", params={"top": {"type": "integer", "description": "自定义", "default": 7}},
          required=["path"], registry=reg)
    def mix(path: str, top: int = 5) -> str:
        """混合。

        Args:
            path: 路径
            top: 次数
        """
        return ""

    p = reg.get("mix").parameters
    assert p["properties"]["top"]["description"] == "自定义", p    # 手写优先
    assert p["properties"]["path"]["description"] == "路径", p      # 未覆盖的仍来自自动推导
    assert p["required"] == ["path"], p
    print("✓ Schema：手写 params 覆盖 + 自动推导兜底，可混用")


def test_json_type_and_full_schema_edge_cases():
    """三个注解/合并的坑：内层 typing. 前缀、PEP 604、旧式整份 schema 丢键。"""
    from core.schema import json_type, merge_params

    # 字符串注解：内层 typing. 前缀也要剥掉
    assert json_type("Optional[typing.List[int]]") == "array"
    assert json_type("typing.Optional[typing.Dict[str, int]]") == "object"
    # PEP 604：int | None 是 types.UnionType，不是 typing.Union
    assert json_type(int | None) == "integer"
    assert json_type(list[str] | None) == "array"

    auto = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    override = {
        "type": "object",
        "title": "自定义",
        "additionalProperties": False,
        "properties": {"b": {"type": "integer"}},
        "required": ["b"],
    }
    got = merge_params(auto, override, None)
    assert got["title"] == "自定义" and got["additionalProperties"] is False, got  # 其余键不丢
    assert got["properties"] == {"b": {"type": "integer"}}, got
    assert got["required"] == ["b"], got
    # required_override 优先于整份 schema 里自带的 required
    assert merge_params(auto, override, ["a", "b"])["required"] == ["a", "b"]
    print("✓ Schema：内层 typing 前缀 / PEP 604 / 整份 schema 键保留与 required 覆盖")


# ============================================================ 2. 注册表语义

def test_registry_semantics():
    reg = ToolRegistry()

    @tool("a", "工具A", group="g1", registry=reg)
    def a() -> str:
        return "a"

    @tool("b", "工具B", group="g2", registry=reg)
    def b() -> str:
        return "b"

    assert len(reg) == 2
    assert reg.groups() == {"g1": [reg.get("a")], "g2": [reg.get("b")]}, reg.groups()
    assert reg.find("工具B")[0].name == "b"
    assert reg.find("g1")[0].name == "a"           # 按分组过滤
    assert len(reg.schemas()) == 2
    assert reg.call("a", {}) == "a"
    assert "[error] 未知工具" in reg.call("nope", {})

    # 冲突检测
    @tool("a", "重复名字", registry=reg)
    def a2() -> str:
        return "x"

    assert "a" in reg.skipped, reg.skipped

    # 按模块热删除
    removed = reg.remove_module(a.__module__)
    assert removed >= 1
    print("✓ 注册表：分组/来源检索、冲突检测、按模块热删除、未知工具兜底 全部正确")


def test_call_converts_errors():
    reg = ToolRegistry()

    @tool("boom", "会炸的工具", registry=reg)
    def boom(x: str = "") -> str:
        raise ValueError("内部错误")

    out = reg.call("boom", {})
    assert out.startswith("[error] ValueError"), out
    out = reg.call("boom", {"不存在的参数": 1})
    assert out.startswith("[error] 参数不匹配"), out
    print("✓ 注册表：工具抛异常 / 参数不匹配 都被转成文本，绝不冒泡")


# ============================================================ 3. 插件加载器

PROBE = "_probe_plugins"


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_loader_discover_reload_and_isolation():
    base = os.path.join(HERE, PROBE)
    if os.path.isdir(base):
        shutil.rmtree(base)
    _write(os.path.join(base, "__init__.py"), "")
    _write(os.path.join(base, "ok_mod.py"), (
        "from core import tool\n"
        "@tool\ndef probe_one(x: str = '') -> str:\n"
        "    '''探针工具一。'''\n"
        "    return 'one'\n"
    ))
    _write(os.path.join(base, "broken_mod.py"), "import 这个模块不存在\n")
    _write(os.path.join(base, "_ignored.py"), "raise RuntimeError('下划线开头应被忽略')\n")

    try:
        reg = ToolRegistry()
        ldr = PluginLoader(HERE, dirs=[PROBE], registry=reg)

        found = ldr.discover()
        assert found == [f"{PROBE}.broken_mod", f"{PROBE}.ok_mod"], found

        rep = ldr.load_all()
        assert f"{PROBE}.ok_mod" in rep.loaded, rep.loaded
        assert f"{PROBE}.broken_mod" in rep.failed, rep.failed
        assert reg.get("probe_one") is not None
        print(f"✓ 加载器：发现 {len(found)} 个模块；坏插件被隔离（{rep.failed[f'{PROBE}.broken_mod'][:30]}…）")

        # 热重载：往同一个文件里加一个工具，reload 后应该变成 2 个
        _write(os.path.join(base, "ok_mod.py"), (
            "from core import tool\n"
            "@tool\ndef probe_one(x: str = '') -> str:\n"
            "    '''探针工具一（改过描述）。'''\n"
            "    return 'one'\n\n"
            "@tool\ndef probe_two(y: str = '') -> str:\n"
            "    '''探针工具二。'''\n"
            "    return 'two'\n"
        ))
        rep2 = ldr.reload(f"{PROBE}.ok_mod")
        assert f"{PROBE}.ok_mod" in rep2.reloaded, rep2.reloaded
        assert reg.get("probe_one") is not None, "重载后 probe_one 不该消失"
        assert reg.get("probe_two") is not None, "新增的 probe_two 应该被注册"
        assert reg.get("probe_one").description == "探针工具一（改过描述）。", "描述应更新"
        print("✓ 加载器：热重载生效（工具数 1→2，旧工具不丢，描述已更新）")

        # 删除插件文件后 reload，工具应被摘掉
        os.remove(os.path.join(base, "ok_mod.py"))
        rep3 = ldr.reload()
        assert f"{PROBE}.ok_mod" in rep3.removed, rep3.removed
        assert reg.get("probe_one") is None and reg.get("probe_two") is None
        print("✓ 加载器：插件文件被删后重载，其工具被正确摘除")

        # 把同名文件加回来：必须真的重新导入（sys.modules 里不能留着旧模块对象）
        _write(os.path.join(base, "ok_mod.py"), (
            "from core import tool\n"
            "@tool\ndef probe_three(z: str = '') -> str:\n"
            "    '''加回来的探针。'''\n"
            "    return 'three'\n"
        ))
        rep4 = ldr.reload(f"{PROBE}.ok_mod")
        assert f"{PROBE}.ok_mod" in rep4.loaded, rep4
        assert reg.get("probe_three") is not None, "删除后重新加回的文件应重新导入并注册"
        print("✓ 加载器：插件删除后再加回，能重新导入（无残留 sys.modules 缓存）")

        # 重载失败（语法错）不能把已有工具清空
        _write(os.path.join(base, "ok_mod.py"), "def broken(:\n")
        rep5 = ldr.reload(f"{PROBE}.ok_mod")
        assert f"{PROBE}.ok_mod" in rep5.failed, rep5
        assert reg.get("probe_three") is not None, "重载失败时旧工具必须保留"
        print("✓ 加载器：重载失败时旧工具被恢复（一次失败不会清空插件能力）")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ============================================================ 4. MCP 通道

def test_mcp_name_sanitization():
    from core.mcp import fq_tool_name

    assert fq_tool_name("srv", "read") == "srv__read"
    assert fq_tool_name("a.b/c", "x y") == "a_b_c__x_y"          # 非法字符被替换
    long_name = fq_tool_name("s" * 60, "t" * 60)
    assert len(long_name) <= 64, len(long_name)                  # 长度受限
    assert long_name != fq_tool_name("s" * 60, "t" * 59), "截断后仍需唯一"
    import re
    assert re.match(r"^[A-Za-z0-9_-]{1,64}$", long_name)
    print("✓ MCP：工具名清洗与截断后仍满足 OpenAI 命名规范且保持唯一")


def test_mcp_end_to_end():
    from core.mcp import MCPManager
    from core.registry import REGISTRY as GLOBAL

    reg = ToolRegistry()
    mgr = MCPManager(
        config_path=os.path.join(HERE, "mcp", "mcp_servers.json"),
        project_root=HERE,
        registry=reg,
    )
    try:
        rep = mgr.load()
        assert "demo" in rep.servers, rep.servers
        st = rep.servers["demo"]
        assert st.status == "ready", f"{st.status}: {st.error}"
        assert st.server_info.get("name") == "mini-agent-demo-mcp", st.server_info
        # 6 个：now / sha256 / env_info / sleep / stubborn_sleep / cancel_log
        # （后两个是为「取消通知」专项测试加的：一个无视取消，一个回显收到的通知）
        assert len(st.tools) == 6, st.tools
        assert "demo__cancel_log" in st.tools, st.tools

        out = reg.call("demo__sha256", {"text": "hello"})
        expect = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert expect in out, out
        assert "2026-" in reg.call("demo__now", {})
        assert "Python" in reg.call("demo__env_info", {})

        # 错误必须被包装成文本，而不是抛异常
        err = reg.call("demo__sha256", {"不存在的参数": 1})
        assert "[mcp error]" in err, err
        print("✓ MCP：真实握手 + tools/list + tools/call + 错误上报 全部走通")
    finally:
        mgr.shutdown()

    # 确认没有污染全局注册表
    assert not [t for t in GLOBAL.all(source="mcp")], "MCP 工具不该注册到全局注册表"


# ============================================================ 5. 危险工具确认

def test_agent_denies_dangerous_tool():
    import agent as agent_mod

    class _UI:
        def __init__(self):
            self.asked = 0

        def turn_start(self, t):
            pass

        def llm_done(self, t, s):
            pass

        def tool_start(self, t, tool, args):
            pass

        def tool_end(self, t, tool, result, s):
            pass

        def ask_confirm(self, tool, args):
            self.asked += 1
            return False       # 用户总是拒绝

    ui = _UI()
    a = agent_mod.Agent(verbose=False, ui=ui, approve=True)
    out = a._execute(1, "run_shell", {"command": "echo hi"})
    assert out.startswith("[denied]"), out
    assert ui.asked == 1

    # 非危险工具不该触发确认
    out = a._execute(1, "list_dir", {"path": "."})
    assert not out.startswith("[denied]"), out
    assert ui.asked == 1, "非危险工具不该询问"
    print("✓ 权限：approve 模式下危险工具被拒并明确告知模型，非危险工具不受影响")


if __name__ == "__main__":
    print("工作区:", HERE)
    print("---")
    test_schema_from_annotations()
    test_schema_optional_and_sphinx()
    test_manual_params_override()
    test_json_type_and_full_schema_edge_cases()
    test_registry_semantics()
    test_call_converts_errors()
    test_loader_discover_reload_and_isolation()
    test_mcp_name_sanitization()
    test_mcp_end_to_end()
    test_agent_denies_dangerous_tool()
    print("---")
    print("全部通过 ✅")
