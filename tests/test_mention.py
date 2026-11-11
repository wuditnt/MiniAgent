"""@ 引用（工作区文件引用与即时上传）后端验证。

覆盖：
  ① 引用路径安全：绝对路径 / 盘符 / `..` / 越界链接一律拒绝
  ② 注入模式：≤8KB 文本内联、>8KB 摘要、图片 data_url、二进制仅路径
  ③ 32KB 总预算：超出后按顺序降级为路径引用
  ④ 快照变更：mtime/size 不一致标记 changed；文件被删也标记
  ⑤ 数量上限：超过 5 个引用 → ValueError
  ⑥ 工作区搜索：跳过忽略目录与点目录、结果/条目截断、路径统一正斜杠
  ⑦ 消息组装：引用文本/图片分片进用户消息，`_references` 元数据可回放
  ⑧ 历史链路：_display_items / _with_pending_user 透出 references
  ⑨ 远程只读：上传与新增引用 403

运行：python test_mention.py
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

import bootstrap  # noqa: F401  —— 必须最先导入，铺 sys.path / 工作区

from core import webui as _webui
import tools  # noqa: F401  —— 先加载工具层，避免 core.agent → tools → subagent 的循环导入
from core.agent import Agent


# ---------------------------------------------------------------- 工具

def _write(root: str, rel: str, data: bytes | str) -> str:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _png_bytes() -> bytes:
    # 1x1 透明 PNG
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
    )


# ---------------------------------------------------------------- 单元

def test_reference_path_safety() -> None:
    root = tempfile.mkdtemp(prefix="mention-path-")
    try:
        _write(root, "docs/a.md", "hello")
        assert _webui._reference_file(root, "docs/a.md") is not None
        assert _webui._reference_file(root, "docs\\a.md") is not None
        assert _webui._reference_file(root, "../escape.md") is None
        assert _webui._reference_file(root, "docs/../../escape.md") is None
        assert _webui._reference_file(root, "/etc/passwd") is None
        assert _webui._reference_file(root, "C:/Windows/win.ini") is None
        assert _webui._reference_file(root, "") is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_resolve_modes() -> None:
    root = tempfile.mkdtemp(prefix="mention-modes-")
    try:
        _write(root, "small.md", "# 标题\n正文内容\n")
        _write(root, "big.md", "行\n" * 4000)                 # > 8KB
        _write(root, "notes.dat", b"\x00\x01\x02binary")
        _write(root, "logo.png", _png_bytes())

        items = _webui._resolve_references([
            {"path": "small.md", "mtime": 0, "size": 0},
            {"path": "big.md", "mtime": 0, "size": 0},
            {"path": "notes.dat", "mtime": 0, "size": 0},
            {"path": "logo.png", "mtime": 0, "size": 0},
        ], root)
        by = {x["path"]: x for x in items}

        assert by["small.md"]["mode"] == "inline", by["small.md"]
        assert "正文内容" in by["small.md"]["content"]
        assert by["big.md"]["mode"] == "path", by["big.md"]
        assert by["big.md"]["summary"].count("\n") >= 1
        assert by["notes.dat"]["mode"] == "path"
        assert by["logo.png"]["mode"] == "image"
        assert by["logo.png"]["data_url"].startswith("data:image/png;base64,")

        # 重复路径按 realpath 去重
        dup = _webui._resolve_references([
            {"path": "small.md"}, {"path": "./small.md"},
        ], root)
        assert len(dup) == 1, dup
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_budget_downgrade() -> None:
    root = tempfile.mkdtemp(prefix="mention-budget-")
    try:
        refs = []
        for i in range(5):
            name = f"f{i}.txt"
            _write(root, name, "x" * 7 * 1024)
            refs.append({"path": name})
        items = _webui._resolve_references(refs, root)
        modes = [x["mode"] for x in items]
        # 32KB / 7KB = 4 个内联，第 5 个必须降级
        assert modes[:4] == ["inline"] * 4, modes
        assert modes[4] == "path", modes
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_changed_and_missing() -> None:
    root = tempfile.mkdtemp(prefix="mention-changed-")
    try:
        fp = _write(root, "a.md", "abc")
        st = os.stat(fp)
        ok = _webui._resolve_references(
            [{"path": "a.md", "mtime": st.st_mtime, "size": st.st_size}], root)
        assert ok[0]["changed"] is False, ok[0]

        changed = _webui._resolve_references(
            [{"path": "a.md", "mtime": st.st_mtime - 100, "size": st.st_size}], root)
        assert changed[0]["changed"] is True, changed[0]

        os.remove(fp)
        gone = _webui._resolve_references([{"path": "a.md"}], root)
        assert gone[0]["changed"] is True and gone[0]["size"] == 0, gone[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reference_limit() -> None:
    root = tempfile.mkdtemp(prefix="mention-limit-")
    try:
        refs = []
        for i in range(6):
            name = f"{i}.txt"
            _write(root, name, "x")
            refs.append({"path": name})
        try:
            _webui._resolve_references(refs, root)
        except ValueError as e:
            assert "最多引用" in str(e), e
        else:
            raise AssertionError("超过 5 个引用应当报错")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_search_skips_ignored_dirs() -> None:
    root = tempfile.mkdtemp(prefix="mention-search-")
    try:
        _write(root, "src/keep_me.py", "print(1)")
        _write(root, "src/deep/inner_keep.md", "x")
        _write(root, "node_modules/pkg/keep_me.js", "x")
        _write(root, ".git/keep_me.txt", "x")
        _write(root, ".hidden/keep_me.txt", "x")

        hits, truncated = _webui._search_workspace(root, "keep", 20)
        paths = {h["path"] for h in hits}
        assert "src/keep_me.py" in paths, paths
        assert "src/deep/inner_keep.md" in paths, paths
        assert not any(p.startswith("node_modules/") for p in paths), paths
        assert not any(p.startswith(".git/") for p in paths), paths
        assert not any(p.startswith(".hidden/") for p in paths), paths
        assert truncated is False

        # 结果上限：造 35 个命中，limit=30 时必须 truncated
        for i in range(35):
            _write(root, f"many/hit_{i}.txt", "x")
        hits, truncated = _webui._search_workspace(root, "hit_", 30)
        assert len(hits) == 30 and truncated is True, (len(hits), truncated)

        # 路径统一正斜杠
        assert all("\\" not in h["path"] for h in hits)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_user_message() -> None:
    atts = []
    refs = [
        {"name": "a.md", "path": "docs/a.md", "mime": "text/markdown",
         "size": 5, "mtime": 1.0, "mode": "inline", "changed": False,
         "content": "hello"},
        {"name": "big.md", "path": "docs/big.md", "mime": "text/markdown",
         "size": 99999, "mtime": 1.0, "mode": "path", "changed": True,
         "summary": "第一行\n第二行"},
        {"name": "p.png", "path": "p.png", "mime": "image/png",
         "size": 10, "mtime": 1.0, "mode": "image", "changed": False,
         "data_url": "data:image/png;base64,AAAA"},
    ]
    msg = Agent._build_user_message("看看这些", atts, refs)
    text = msg["content"][0]["text"] if isinstance(msg["content"], list) else msg["content"]
    assert "[引用文件 docs/a.md 内容]\nhello" in text, text
    assert "docs/big.md" in text and "未内联" in text
    assert "第一行" in text
    assert "[引用图片 p.png]" in text
    assert len(msg["content"]) == 2, msg["content"]           # text + 1 个图片分片
    meta = msg["_references"]
    assert [m["path"] for m in meta] == ["docs/a.md", "docs/big.md", "p.png"]
    assert "content" not in meta[0] and "data_url" not in meta[2]

    # 只有引用、没有正文也允许
    only = Agent._build_user_message("", [], [refs[0]])
    assert only["content"].startswith("\n\n[引用文件"), only["content"]


def test_history_items() -> None:
    raw = [{
        "role": "user", "content": "看一下", "_references": [
            {"name": "a.md", "path": "a.md", "mode": "inline"},
        ],
    }]
    items = _webui._display_items(raw)
    assert items and items[0]["references"][0]["path"] == "a.md", items

    class _S:
        pending_turn = {
            "turn": 1, "message": "看一下", "attachments": [],
            "references": [{"path": "a.md"}], "ts": 1.0,
        }
    out = _webui._with_pending_user(_S(), [])
    assert out[-1]["_references"][0]["path"] == "a.md", out


# ---------------------------------------------------------------- HTTP

def test_remote_readonly() -> None:
    port = 8791
    tmp = tempfile.mkdtemp(prefix="mention-http-")
    old_sessions = _webui._SESSIONS_DIR
    wsrv = None
    try:
        _webui._SESSIONS_DIR = os.path.join(tmp, "sessions")
        wsrv = _webui.WebServer(port=port, model="mock-gpt", login_enabled=False)
        threading.Thread(target=wsrv.serve_forever, daemon=True).start()

        def call(method: str, path: str, payload: dict | None = None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=data, method=method,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        for _ in range(50):
            try:
                if call("GET", "/api/health")[0] == 200:
                    break
            except Exception:  # noqa: BLE001 —— 服务还没起来
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("WebServer 未能在 5s 内就绪")

        # 本机：stat 可用（不存在文件 exists=false）
        status, body = call("POST", "/api/workspace/stat",
                            {"workspace_id": "default", "paths": ["no-such.md"]})
        assert status == 200 and body["items"][0]["exists"] is False, (status, body)

        # 模拟远程来源
        original = _webui._RequestHandler._is_remote
        _webui._RequestHandler._is_remote = lambda self: True
        try:
            status, body = call("POST", "/api/upload",
                                {"name": "a.txt", "data": "eA==", "mime": "text/plain"})
            assert status == 403, (status, body)

            status, body = call("POST", "/api/chat", {
                "session": "mention-remote", "message": "hi",
                "references": [{"path": "a.md"}],
            })
            assert status == 403, (status, body)
        finally:
            _webui._RequestHandler._is_remote = original
    finally:
        if wsrv is not None:
            wsrv.shutdown()
        _webui._SESSIONS_DIR = old_sessions
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    cases = [
        ("引用路径安全", test_reference_path_safety),
        ("注入模式", test_resolve_modes),
        ("32KB 预算降级", test_budget_downgrade),
        ("快照变更标记", test_changed_and_missing),
        ("引用数量上限", test_reference_limit),
        ("工作区搜索", test_search_skips_ignored_dirs),
        ("消息组装", test_build_user_message),
        ("历史回放透出", test_history_items),
        ("远程只读", test_remote_readonly),
    ]
    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"  [{name}] 通过")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  [{name}] 失败：{type(e).__name__}: {e}")
            traceback.print_exc()
    print("=" * 60)
    print(f"{len(cases) - failed}/{len(cases)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
