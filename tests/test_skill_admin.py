"""
技能管理接口测试（脚本型）。不需要 API Key，不花任何费用。

覆盖：
  1. GET  /api/skills          —— id / enabled / editable 字段
  2. GET  /api/skills/detail   —— 正文、路径；未知 id 404
  3. PUT  /api/skills/content  —— 自定义可改、内置 403、重名 400
  4. POST /api/skills/enabled  —— 停用后不进清单、状态落盘
  5. DELETE /api/skills        —— 软删除到 .trash、内置 403、删除后 404

本模块自带服务，直接用 pytest 收集会得到「连接被拒绝」的假失败，
因此只在 `python tests/test_skill_admin.py` 下运行。
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
from urllib.parse import quote

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core.skills import SkillLoader  # noqa: E402
from core.webui import WebServer  # noqa: E402

if __name__ != "__main__":
    import pytest

    pytest.skip(
        "脚本型用例需自带服务：python tests/test_skill_admin.py（或 python tests/run_tests.py）",
        allow_module_level=True,
    )

PORT = 8792
BASE = f"http://127.0.0.1:{PORT}"

BUILTIN_ID = "builtin:alpha"
CUSTOM_ID = "custom:my-one"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _req(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def _make_project() -> str:
    root = tempfile.mkdtemp(prefix="ma-skill-admin-")
    _write(os.path.join(root, "skills", "alpha", "SKILL.md"),
           "---\nname: alpha\ndescription: 内置技能\n---\n\n# 内置正文")
    _write(os.path.join(root, "skills_user", "my-one", "SKILL.md"),
           "---\nname: my-one\ndescription: 自定义技能\n---\n\n# 自定义正文")
    return root


def test_list_fields():
    code, data = _req("GET", "/api/skills")
    assert code == 200, data
    by_id = {s["id"]: s for s in data["skills"]}
    assert BUILTIN_ID in by_id and CUSTOM_ID in by_id, by_id
    builtin = by_id[BUILTIN_ID]
    assert builtin["source"] == "builtin" and builtin["editable"] is False
    assert builtin["enabled"] is True
    assert by_id[CUSTOM_ID]["editable"] is True
    print("✓ 列表：id / enabled / editable 字段正确")


def test_detail():
    code, data = _req("GET", f"/api/skills/detail?id={quote(CUSTOM_ID)}")
    assert code == 200, data
    assert data["skill"]["name"] == "my-one"
    assert "# 自定义正文" in data["content"]
    # content 保留 frontmatter 供编辑；body 已剥离，供 Markdown 渲染
    assert data["body"].startswith("# 自定义正文"), data["body"]
    assert "description:" not in data["body"], data["body"]
    assert data["path"].endswith("skills_user/my-one") or data["path"].endswith("skills_user\\my-one")

    code, _ = _req("GET", "/api/skills/detail?id=" + quote("custom:nope"))
    assert code == 404, code
    print("✓ 详情：正文与路径正确，未知 id → 404")


def test_toggle_and_index():
    code, data = _req("POST", "/api/skills/enabled", {"id": BUILTIN_ID, "enabled": False})
    assert code == 200 and data["skill"]["enabled"] is False, data

    code, data = _req("GET", "/api/skills")
    assert next(s for s in data["skills"] if s["id"] == BUILTIN_ID)["enabled"] is False

    code, _ = _req("POST", "/api/skills/enabled", {"id": "custom:nope", "enabled": False})
    assert code == 404, code

    code, _ = _req("POST", "/api/skills/enabled", {"id": BUILTIN_ID, "enabled": True})
    assert code == 200
    print("✓ 启停：状态生效并落盘，未知 id → 404")


def test_save():
    new_text = "---\nname: my-one\ndescription: 改过的描述\n---\n\n# 新正文\n第一步"

    # 系统内置不可编辑
    code, data = _req("PUT", "/api/skills/content",
                      {"id": BUILTIN_ID, "content": "---\nname: alpha\n---\n\nx"})
    assert code == 403, (code, data)

    # 自定义可编辑
    code, data = _req("PUT", "/api/skills/content",
                      {"id": CUSTOM_ID, "content": new_text})
    assert code == 200, data
    assert data["skill"]["description"] == "改过的描述", data

    code, data = _req("GET", f"/api/skills/detail?id={quote(CUSTOM_ID)}")
    assert "# 新正文" in data["content"]

    # 重名拦截
    code, data = _req("PUT", "/api/skills/content",
                      {"id": CUSTOM_ID, "content": "---\nname: alpha\n---\n\n撞名"})
    assert code == 400, (code, data)

    # 空内容拦截
    code, _ = _req("PUT", "/api/skills/content", {"id": CUSTOM_ID, "content": "   "})
    assert code == 400, code
    print("✓ 保存：自定义可改、内置 403、重名/空内容 400")


def test_delete_soft(project_root: str):
    # 内置不可删除
    code, _ = _req("DELETE", f"/api/skills?id={quote(BUILTIN_ID)}")
    assert code == 403, code

    code, data = _req("DELETE", f"/api/skills?id={quote(CUSTOM_ID)}")
    assert code == 200, data
    assert ".trash" in data["trash"], data

    trash = os.path.join(project_root, "skills_user", ".trash")
    assert os.path.isdir(trash) and os.listdir(trash), "技能目录应移到 .trash"

    code, data = _req("GET", "/api/skills")
    assert CUSTOM_ID not in {s["id"] for s in data["skills"]}, data
    code, _ = _req("GET", f"/api/skills/detail?id={quote(CUSTOM_ID)}")
    assert code == 404, code
    print("✓ 删除：软删除到 .trash，内置 403，删除后列表/详情同步")


def main() -> int:
    root = _make_project()
    state = os.path.join(root, "skills_state.json")
    loader = SkillLoader(root, dirs=["skills", "skills_user"], state_path=state)
    loader.load_all()

    srv = WebServer(host="127.0.0.1", port=PORT, login_enabled=False, skill_loader=loader)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/state", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError("测试服务未启动")

    try:
        test_list_fields()
        test_detail()
        test_toggle_and_index()
        test_save()
        test_delete_soft(root)
    finally:
        if srv._httpd is not None:
            srv._httpd.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    print("---")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
