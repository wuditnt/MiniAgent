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

    # 停用
    code, data = _req("POST", "/api/connectors/toggle", {"name": "demo2", "enabled": False})
    assert code == 200 and data["connector"]["enabled"] is False, data
    assert data["connector"]["tool_count"] == 0, data

    # 删除
    code, data = _req("DELETE", "/api/connectors?name=demo2")
    assert code == 200 and data["removed"] is True, data
    code, data = _req("GET", "/api/connectors")
    assert {c["name"] for c in data["connectors"]} == {"demo"}, data

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
        test_migration(tmp)
        test_manager()
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
