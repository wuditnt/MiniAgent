"""连接器（MCP 外部能力）测试（脚本型）。不需要 API Key，不花任何费用。

覆盖：
  1. connector_store.normalize   —— 默认值、参数解析、非法输入
  2. connector_store CRUD        —— 落盘位置、启停、删除、坏文件兜底
  3. 首次迁移                    —— 从安装目录 mcp_servers.json 复制到数据目录
  4. MCPManager 热加载           —— reload / snapshot / test_spec 与注册表增删
  5. /api/connectors 五个接口    —— 列表 / 保存 / 试连 / 启停 / 删除 / 未启用 503

和技能管理用例一样自带 HTTP 服务，所以只在 `python tests/test_connectors.py`
（或 `python tests/run_tests.py`）下运行，直接交给 pytest 收集会连不上。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import connector_store  # noqa: E402
from core.mcp import MCPManager  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402
from core.webui import WebServer  # noqa: E402

if __name__ != "__main__":
    import pytest

    pytest.skip(
        "脚本型用例需自带服务：python tests/test_connectors.py（或 python tests/run_tests.py）",
        allow_module_level=True,
    )

ROOT = bootstrap.PACKAGE_ROOT
PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"

DEMO_SPEC = {
    "display_name": "演示 MCP",
    "description": "协议测试用",
    "transport": "stdio",
    "command": "python",
    "args": ["mcp/mcp_demo_server.py"],
    "cwd": ".",
    "timeout": 20,
    "enabled": True,
    "dangerous": False,
}


def _req(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


# ============================================================ 1. 校验

def test_normalize():
    spec = connector_store.normalize(
        "my-tools", {"command": " npx ", "args": "-y\n@x/y\n\n", "description": " hi "}
    )
    assert spec["command"] == "npx", spec
    assert spec["args"] == ["-y", "@x/y"], spec
    assert spec["display_name"] == "my-tools"
    assert spec["description"] == "hi"
    assert spec["transport"] == "stdio"
    assert spec["timeout"] == 30.0
    assert spec["enabled"] is True and spec["dangerous"] is True

    bad = [
        ("", {"command": "python"}),
        ("has space", {"command": "python"}),
        ("ok", {"command": ""}),
        ("ok", {"command": "python", "transport": "http"}),
        ("ok", {"command": "python", "timeout": 0}),
        ("ok", {"command": "python", "timeout": 9999}),
        ("ok", {"command": "python", "env": ["KEY=1"]}),
        ("ok", {"command": "python", "args": 1}),
    ]
    for name, raw in bad:
        try:
            connector_store.normalize(name, raw)
        except connector_store.ConnectorError:
            continue
        raise AssertionError(f"应当拒绝：{name} {raw}")

    # 工具级开关：去重、去空白，非法类型拒绝
    spec = connector_store.normalize(
        "tools-off", {"command": "python", "disabled_tools": [" echo ", "echo"]}
    )
    assert spec["disabled_tools"] == ["echo"], spec
    for bad_tools in (5, ["ok", 1]):
        try:
            connector_store.normalize("bad", {"command": "python", "disabled_tools": bad_tools})
        except connector_store.ConnectorError:
            continue
        raise AssertionError(f"disabled_tools 非法时应当拒绝：{bad_tools}")

    # HTTP 传输：url 必填且必须是 http(s)，headers 只接受键值对象
    http_spec = connector_store.normalize(
        "remote",
        {
            "transport": "http",
            "url": " https://example.com/mcp ",
            "headers": {"Authorization": "Bearer x"},
            "timeout": 10,
        },
    )
    assert http_spec["transport"] == "http"
    assert http_spec["url"] == "https://example.com/mcp"
    assert http_spec["headers"] == {"Authorization": "Bearer x"}
    assert http_spec["command"] == ""

    for raw in (
        {"transport": "http", "url": ""},
        {"transport": "http", "url": "ftp://example.com/mcp"},
        {"transport": "http", "url": "https://example.com/mcp", "headers": ["a"]},
    ):
        try:
            connector_store.normalize("remote", raw)
        except connector_store.ConnectorError:
            continue
        raise AssertionError(f"应当拒绝：{raw}")

    # 密钥掩码：GET 只回 ***末4位，保存时命中掩码即保留原值
    assert connector_store.mask_value("abcdefgh") == "***efgh"
    assert connector_store.mask_value("abc") == "***"
    assert connector_store.mask_value("") == ""
    env, lost = connector_store.merge_masked_env(
        {"TOKEN": "real-secret-1234"}, {"TOKEN": "***1234", "NEW": "***"}
    )
    assert env == {"TOKEN": "real-secret-1234"}, env
    assert lost == ["NEW"], lost

    # python/py 会被 MCP 改写成应用解释器，所以界面永远显示「命令已找到」
    assert connector_store.command_available("python") is True
    assert connector_store.command_available("definitely-not-a-real-command-xyz") is False
    print("✓ 校验：默认值 / 参数切行 / 非法输入全部按预期")


# ============================================================ 2. CRUD

def test_store():
    path = connector_store.config_path()
    assert os.path.dirname(path) == os.environ["MINI_AGENT_DATA_DIR"], path

    connector_store.upsert("demo", DEMO_SPEC)
    connector_store.upsert("other", {"command": "python", "enabled": False})
    data = connector_store.load()
    assert set(data["servers"]) == {"demo", "other"}, data
    assert os.path.isfile(path), "配置必须落到数据目录"

    spec = connector_store.set_enabled("other", True)
    assert spec["enabled"] is True
    assert connector_store.load()["servers"]["other"]["enabled"] is True

    assert connector_store.remove("other") is True
    assert connector_store.remove("other") is False
    assert "other" not in connector_store.load()["servers"]

    try:
        connector_store.set_enabled("nope", True)
    except connector_store.ConnectorError:
        pass
    else:
        raise AssertionError("未知连接器应报错")

    # 配置文件被写坏时不能让页面打不开：读成空表
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert connector_store.load()["servers"] == {}
    print("✓ 存储：落盘 / 启停 / 删除 / 坏文件兜底 全部正确")


# ============================================================ 2.5 全局默认勾选

def test_defaults():
    connector_store.save({"servers": {
        "alpha": {"command": "python"},
        "beta": {"command": "python"},
        "off": {"command": "python", "enabled": False},
    }})
    # 未设置（null）= 继承全部已启用连接器；停用的永不生效
    assert connector_store.get_defaults() is None
    assert connector_store.resolve_selection(None) == ["alpha", "beta"]

    # 显式设置：过滤不存在的名字并去重
    assert connector_store.set_defaults(["alpha", "ghost", "alpha"]) == ["alpha"]
    assert connector_store.get_defaults() == ["alpha"]
    assert connector_store.resolve_selection(None) == ["alpha"]

    # 会话显式勾选优先，仍与 enabled 求交集
    assert connector_store.resolve_selection(["beta", "off", "ghost"]) == ["beta"]
    assert connector_store.resolve_selection([]) == []

    # 删除连接器时从默认列表里摘掉
    connector_store.remove("alpha")
    assert connector_store.get_defaults() == []
    print("✓ 默认勾选：null 继承 / 显式过滤 / 停用排除 / 删除清理 全部正确")


# ============================================================ 2.6 工具表按会话过滤

def test_registry_scope():
    from core.registry import Tool

    reg = ToolRegistry()
    reg.add(Tool(name="read_file", description="", parameters={}, func=lambda: ""))
    reg.add(Tool(name="demo__echo", description="", parameters={}, func=lambda: "",
                 module="mcp:demo", source="mcp", group="MCP:demo"))
    reg.add(Tool(name="other__x", description="", parameters={}, func=lambda: "",
                 module="mcp:other", source="mcp", group="MCP:other"))

    def names(include):
        return {s["function"]["name"] for s in reg.schemas(include_modules=include)}

    assert names(None) == {"read_file", "demo__echo", "other__x"}
    assert names({"mcp:demo"}) == {"read_file", "demo__echo"}
    # 空集合 = 只用内置工具（MCP 全不可见）
    assert names(set()) == {"read_file"}

    # 派发兜底：即使模型硬造出未勾选的工具名，也不允许真正执行
    import agent  # noqa: F401  —— 先跑完工具表的循环导入，再取 Agent
    from core.agent import Agent
    from core.registry import Tool as ToolCls

    def _t(module: str) -> ToolCls:
        return ToolCls(name="x", description="", parameters={}, func=lambda: "", module=module)

    a = Agent.__new__(Agent)  # 只验证纯函数，不初始化模型/客户端
    a.connector_scope = {"mcp:demo"}
    assert a._connector_allowed(_t("mcp:demo")) is True
    assert a._connector_allowed(_t("mcp:other")) is False
    assert a._connector_allowed(_t("fs_tools")) is True   # 内置工具不受影响
    a.connector_scope = set()
    assert a._connector_allowed(_t("mcp:demo")) is False
    a.connector_scope = None                              # 未启用会话隔离 → 全放行
    assert a._connector_allowed(_t("mcp:other")) is True
    print("✓ 工具表：include_modules 只过滤 MCP，内置工具始终可见；派发越界被拦截")


# ============================================================ 3. 迁移

def test_migration(tmp: str):
    old = os.path.join(tmp, "legacy-mcp.json")
    with open(old, "w", encoding="utf-8") as f:
        json.dump({"servers": {"legacy": {"command": "python", "args": ["x.py"]}}}, f)

    saved = connector_store._legacy_path
    connector_store._legacy_path = lambda: old  # type: ignore[assignment]
    try:
        os.remove(connector_store.config_path())
        data = connector_store.load()
        assert "legacy" in data["servers"], data
        assert os.path.isfile(connector_store.config_path()), "迁移后应写入新位置"

        # 已有新配置时不覆盖
        connector_store.upsert("mine", {"command": "python"})
        data = connector_store.load()
        assert "mine" in data["servers"] and "legacy" in data["servers"], data
    finally:
        connector_store._legacy_path = saved  # type: ignore[assignment]
    print("✓ 迁移：旧配置首次导入，已有配置不被覆盖")


# ============================================================ 4. 热加载

def test_manager():
    connector_store.save({"servers": {"demo": DEMO_SPEC}})
    reg = ToolRegistry()
    mgr = MCPManager(connector_store.config_path(), ROOT, reg)
    try:
        rep = mgr.load()
        st = rep.servers["demo"]
        assert st.status == "ready", f"{st.status}: {st.error}"
        assert len(st.tools) == 6, st.tools

        snap = mgr.snapshot()
        item = next(s for s in snap["servers"] if s["name"] == "demo")
        assert item["tool_count"] == 6 and item["status"] == "ready", item
        assert item["tool_total"] == 6 and item["started_ms"] >= 0, item

        # 工具级开关：被禁用的工具不注册，但快照里仍能看到（enabled=False）
        connector_store.upsert("demo", {**DEMO_SPEC, "disabled_tools": ["sha256"]})
        mgr.reload("demo")
        mcp_tools = {t.name for t in reg.all(source="mcp")}
        assert len(mcp_tools) == 5 and "demo__sha256" not in mcp_tools, mcp_tools
        item = next(s for s in mgr.snapshot()["servers"] if s["name"] == "demo")
        assert item["tool_count"] == 5 and item["tool_total"] == 6, item
        assert next(t for t in item["tools"] if t["raw_name"] == "sha256")["enabled"] is False
        connector_store.upsert("demo", DEMO_SPEC)
        mgr.reload("demo")

        # 启动失败：自动重试一次，错误与尝试次数可见
        connector_store.upsert("bad", {"command": "definitely-not-a-real-command-xyz"})
        mgr.reload("bad")
        bad = next(s for s in mgr.snapshot()["servers"] if s["name"] == "bad")
        assert bad["status"] == "failed" and bad["attempts"] == 2, bad
        assert "已自动重试" in bad["error"], bad
        connector_store.remove("bad")
        mgr.reload("bad")

        # 试连：临时进程，不写注册表
        before = len(reg.all(source="mcp"))
        result = mgr.test_spec({**DEMO_SPEC, "name": "demo"})
        assert result["ok"] is True, result
        assert len(result["tools"]) == 6, result["tools"]
        assert len(reg.all(source="mcp")) == before, "试连不应改动注册表"

        # 停用 → 工具下线
        connector_store.set_enabled("demo", False)
        mgr.reload("demo")
        assert reg.all(source="mcp") == []
        assert mgr.snapshot()["servers"][0]["tool_count"] == 0

        # 再启用 → 工具回来
        connector_store.set_enabled("demo", True)
        mgr.reload("demo")
        assert len(reg.all(source="mcp")) == 6

        # 删除 → 连接与工具一并清理
        connector_store.remove("demo")
        mgr.reload("demo")
        assert reg.all(source="mcp") == []
        assert mgr.snapshot()["servers"] == []
    finally:
        mgr.shutdown()
    print("✓ 热加载：ready / 试连无副作用 / 停用下线 / 启用恢复 / 删除清理")


# ============================================================ 5. HTTP 接口

def test_http(mgr: MCPManager):
    connector_store.save({"servers": {"demo": DEMO_SPEC}})
    mgr.load()

    code, data = _req("GET", "/api/connectors")
    assert code == 200, data
    assert data["mcp_enabled"] is True
    assert len(data["connectors"]) == 1 and data["connectors"][0]["name"] == "demo", data
    assert data["connectors"][0]["tool_count"] == 6, data["connectors"][0]
    assert "node" in data["runtime"] and "npx" in data["runtime"], data["runtime"]

    # 非法标识 → 400
    code, data = _req("POST", "/api/connectors", {"name": "bad name", "command": "python"})
    assert code == 400, (code, data)

    # 新增第二个连接器：保存后立即热加载
    code, data = _req("POST", "/api/connectors", {**DEMO_SPEC, "name": "demo2"})
    assert code == 200 and data["connector"]["status"] == "ready", data
    assert data["connector"]["tool_count"] == 6, data

    # 试连
    code, data = _req("POST", "/api/connectors/test", {**DEMO_SPEC, "name": "dry"})
    assert code == 200 and data["ok"] is True, data
    assert len(data["tools"]) == 6, data

    # 工具级开关 + 密钥掩码：保存后工具变 5 个，GET 只回 ***末 4 位
    code, data = _req("POST", "/api/connectors", {
        **DEMO_SPEC,
        "name": "demo3",
        "disabled_tools": ["now"],
        "env": {"TOKEN": "secret-value-9876"},
    })
    assert code == 200, data
    assert data["connector"]["tool_count"] == 5 and data["connector"]["tool_total"] == 6, data
    assert data["connector"]["env"]["TOKEN"] == "***9876", data

    # 用掩码回传保存：磁盘上的真实值必须保留
    code, data = _req("POST", "/api/connectors", {
        **DEMO_SPEC,
        "name": "demo3",
        "disabled_tools": ["now"],
        "env": {"TOKEN": "***9876"},
    })
    assert code == 200, data
    real_env = connector_store.load()["servers"]["demo3"]["env"]
    assert real_env["TOKEN"] == "secret-value-9876", real_env

    # 重试接口：只重连，不改配置
    code, data = _req("POST", "/api/connectors/retry", {"name": "demo3"})
    assert code == 200 and data["connector"]["status"] == "ready", (code, data)

    # 导出：env 脱敏，可安全分享
    code, data = _req("GET", "/api/connectors/export")
    assert code == 200 and data["servers"]["demo3"]["env"]["TOKEN"] == "***9876", data

    # 导入：新连接器的掩码 env 无从还原 → 跳过并给警告
    code, data = _req("POST", "/api/connectors/import", {
        "servers": {"imported": {**DEMO_SPEC, "env": {"TOKEN": "***9876"}}},
    })
    assert code == 200 and any(r["name"] == "imported" and r["ok"] for r in data["results"]), data
    assert data["warnings"] and "TOKEN" in data["warnings"][0], data
    _req("DELETE", "/api/connectors?name=demo3")
    _req("DELETE", "/api/connectors?name=imported")

    # 停用
    code, data = _req("POST", "/api/connectors/toggle", {"name": "demo2", "enabled": False})
    assert code == 200 and data["connector"]["enabled"] is False, data
    assert data["connector"]["tool_count"] == 0, data

    # 删除
    code, data = _req("DELETE", "/api/connectors?name=demo2")
    assert code == 200 and data["removed"] is True, data
    code, data = _req("GET", "/api/connectors")
    assert {c["name"] for c in data["connectors"]} == {"demo"}, data

    # 会话级勾选：写会话 JSON，本机管理员同时同步全局默认
    from core import webui as webui_mod

    sid = "conn-test"
    webui_mod._save_session_file(
        sid, {"id": sid, "title": "t", "workspace_id": "default", "messages": []}
    )
    code, data = _req(
        "POST", f"/api/sessions/{sid}/connectors", {"connectors": ["demo"]}
    )
    assert code == 200 and data["connectors"] == ["demo"], (code, data)
    assert data["effective"] == ["demo"], data
    assert connector_store.get_defaults() == ["demo"], connector_store.get_defaults()

    # 未知连接器 → 400，不改动会话
    code, data = _req(
        "POST", f"/api/sessions/{sid}/connectors", {"connectors": ["ghost"]}
    )
    assert code == 400, (code, data)
    assert webui_mod._load_session_file(sid)["connectors"] == ["demo"]

    # null = 回到「跟全局默认」
    code, data = _req(
        "POST", f"/api/sessions/{sid}/connectors", {"connectors": None}
    )
    assert code == 200 and data["connectors"] is None, (code, data)
    assert data["effective"] == ["demo"], data

    # 停用的连接器即使被显式勾选也不生效
    connector_store.set_enabled("demo", False)
    assert connector_store.resolve_selection(["demo"]) == []
    connector_store.set_enabled("demo", True)

    # --no-mcp：只读展示，写操作 503
    srv_mcp = mgr
    # 通过 handler 所属的 WebServer 关掉 mcp 再恢复
    server = _SERVER[0]
    server.mcp = None
    try:
        code, data = _req("GET", "/api/connectors")
        assert code == 200 and data["mcp_enabled"] is False, data
        code, data = _req("POST", "/api/connectors", {**DEMO_SPEC, "name": "x"})
        assert code == 503, (code, data)
    finally:
        server.mcp = srv_mcp
    print("✓ 接口：列表 / 保存 / 试连 / 启停 / 删除 / --no-mcp 503 全部正确")


# ============================================================ 5.5 内置模板

def test_presets():
    from core import connector_presets

    connector_presets._cache = None  # 保证读到的是当前包内文件
    presets = connector_presets.all()
    assert len(presets) == 8, presets
    keys = {p["key"] for p in presets}
    assert keys == {
        "filesystem", "fetch", "memory", "git", "sqlite", "time",
        "puppeteer", "sequential-thinking",
    }, keys
    assert all(p["command"] and p["args"] for p in presets), presets
    assert connector_presets.get("git")["args"], "模板参数不能为空"

    code, data = _req("GET", "/api/connectors/presets")
    assert code == 200 and len(data["presets"]) == 8, (code, data)
    print("✓ 模板：8 个内置模板可加载，接口返回完整清单")


_SERVER: list[WebServer] = []


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ma-connector-test-")
    os.environ["MINI_AGENT_DATA_DIR"] = tmp

    connector_store.save({"servers": {}})

    reg = ToolRegistry()
    mgr = MCPManager(connector_store.config_path(), ROOT, reg)
    srv = WebServer(host="127.0.0.1", port=PORT, login_enabled=False, mcp_manager=mgr)
    _SERVER.append(srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/state", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        mgr.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("测试服务未启动")

    try:
        test_normalize()
        test_store()
        test_defaults()
        test_registry_scope()
        test_migration(tmp)
        test_manager()
        test_presets()
        test_http(mgr)
    finally:
        if srv._httpd is not None:
            srv._httpd.shutdown()
        mgr.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("---")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
