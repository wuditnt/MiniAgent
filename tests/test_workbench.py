"""
工作台文件浏览接口测试。不需要 API Key，不花任何费用。

覆盖：
  1. 根目录列表 —— 目录优先、名称排序、隐藏 .memory/.attachments
  2. 子目录导航 —— path/parent 正确
  3. 路径安全 —— ../、绝对路径、.memory 全部拒绝
  4. 文本读取 —— UTF-8 / GBK 编码与行数
  5. Markdown —— 按文本返回
  6. 图片 —— kind=image + raw 接口 MIME 正确
  7. 二进制 —— kind=binary
  8. 大文件 —— 行数截断标记
  9. 缺失文件 / 非图片 raw —— 404 / 403
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
from bootstrap import PACKAGE_ROOT as HERE

import core.workspace_config as workspace_config  # noqa: E402
from core.webui import WebServer  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


def _get(path: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _json(path: str):
    code, _ctype, body = _get(path)
    return code, json.loads(body.decode("utf-8"))


def _api_dir(wid: str = "default", path: str = ""):
    return _json(f"/api/workspace/dir?workspace_id={wid}&path={quote(path)}")


def _api_file(path: str, wid: str = "default"):
    return _json(f"/api/workspace/file?workspace_id={wid}&path={quote(path)}")


def _make_workspace() -> str:
    ws = tempfile.mkdtemp(prefix="ma-wb-")
    with open(os.path.join(ws, "a.py"), "w", encoding="utf-8") as f:
        f.write("print('hello')\n")
    with open(os.path.join(ws, "notes.md"), "w", encoding="utf-8") as f:
        f.write("# 标题\n\n正文\n")
    with open(os.path.join(ws, "gbk.txt"), "wb") as f:
        f.write("中文内容\n第二行".encode("gbk"))
    with open(os.path.join(ws, "bin.dat"), "wb") as f:
        f.write(b"\x00\x01\x02binary")
    with open(os.path.join(ws, "big.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(f"line {i}" for i in range(6000)))
    with open(os.path.join(ws, "pic.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    with open(os.path.join(ws, "page.html"), "w", encoding="utf-8") as f:
        f.write(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="style.css">'
            "</head><body><h1 id=\"t\">hi</h1>"
            '<img src="pic.png"><script src="app.js"></script>'
            "</body></html>"
        )
    with open(os.path.join(ws, "style.css"), "w", encoding="utf-8") as f:
        f.write("body { color: red; }\n")
    with open(os.path.join(ws, "app.js"), "w", encoding="utf-8") as f:
        f.write("document.getElementById('t').textContent = 'ok';\n")
    _make_mhtml(ws)
    os.makedirs(os.path.join(ws, "sub"))
    with open(os.path.join(ws, "sub", "b.txt"), "w", encoding="utf-8") as f:
        f.write("in sub\n")
    os.makedirs(os.path.join(ws, ".memory"))
    with open(os.path.join(ws, ".memory", "project.md"), "w", encoding="utf-8") as f:
        f.write("secret")
    os.makedirs(os.path.join(ws, ".attachments"))
    with open(os.path.join(ws, ".attachments", "x.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    with open(os.path.join(ws, "..", "ma-wb-secret.txt"), "w", encoding="utf-8") as f:
        f.write("outside")
    return ws


def _make_mhtml(ws: str) -> None:
    """构造一个 multipart/related MHTML：HTML + 图片(cid) + CSS(Content-Location)。"""
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("related", type="text/html")
    html = MIMEText(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css"></head>'
        '<body><img src="cid:img1@test"></body></html>',
        "html",
        "utf-8",
    )
    html.add_header("Content-Location", "page.html")
    msg.attach(html)

    img = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"0" * 32, "png")
    img.add_header("Content-ID", "<img1@test>")
    img.add_header("Content-Location", "pic.png")
    msg.attach(img)

    css = MIMEText("body { color: red; }", "css", "utf-8")
    css.add_header("Content-Location", "style.css")
    msg.attach(css)

    data = msg.as_bytes()
    for name in ("page.mht", "page.mhtml"):
        with open(os.path.join(ws, name), "wb") as f:
            f.write(data)

    # 没有 HTML 主体的归档
    bad = MIMEMultipart("related", type="image/png")
    bad.attach(MIMEImage(b"\x89PNG\r\n\x1a\n", "png"))
    with open(os.path.join(ws, "nohtml.mht"), "wb") as f:
        f.write(bad.as_bytes())


def test_root_listing():
    code, data = _api_dir()
    assert code == 200, data
    names = [e["name"] for e in data["entries"]]
    assert names[:1] == ["sub"], names
    assert ".memory" not in names and ".attachments" not in names, names
    assert "a.py" in names and "notes.md" in names
    assert data["path"] == "" and data["parent"] == ""
    assert all({"name", "path", "type", "size", "mtime", "ext"} <= set(e) for e in data["entries"])
    print("✓ 根目录：目录优先、名称排序、隐藏 .memory/.attachments")


def test_subdir_and_nav():
    code, data = _api_dir(path="sub")
    assert code == 200, data
    assert data["path"] == "sub" and data["parent"] == ""
    assert [e["name"] for e in data["entries"]] == ["b.txt"]
    print("✓ 子目录：path/parent 与条目正确")


def test_path_security():
    code, _ = _api_dir(path="../ma-wb-secret.txt")
    assert code == 403, code
    code, _ = _api_dir(path="..")
    assert code == 403, code
    absolute = os.path.abspath(os.sep + "Windows")
    code, _ = _api_dir(path=absolute)
    assert code == 400, code
    code, _ = _api_file(".memory/project.md")
    assert code == 403, code
    print("✓ 路径安全：../、绝对路径、.memory 均被拒绝")


def test_text_and_encoding():
    code, data = _api_file("a.py")
    assert code == 200 and data["kind"] == "text", data
    assert data["content"] == "print('hello')", data["content"]
    assert data["encoding"].startswith("utf-8"), data["encoding"]

    code, data = _api_file("gbk.txt")
    assert code == 200 and data["kind"] == "text", data
    assert data["content"] == "中文内容\n第二行", data["content"]
    assert data["encoding"] == "gbk"
    print("✓ 文本读取：UTF-8 / GBK 解码正确")


def test_markdown_and_image():
    code, data = _api_file("notes.md")
    assert code == 200 and data["kind"] == "text" and data["ext"] == ".md", data

    code, data = _api_file("pic.png")
    assert code == 200 and data["kind"] == "image", data
    assert data["url"].startswith("/api/workspace/raw?"), data
    code, ctype, body = _get(data["url"])
    assert code == 200 and ctype.startswith("image/png") and body.startswith(b"\x89PNG"), (code, ctype)
    print("✓ Markdown 按文本返回；图片 raw 接口 MIME 正确")


def test_binary_and_truncation():
    code, data = _api_file("bin.dat")
    assert code == 200 and data["kind"] == "binary", data

    code, data = _api_file("big.txt")
    assert code == 200 and data["kind"] == "text", data
    assert data["truncated"] is True, data
    assert data["lines"] == 5000, data["lines"]
    print("✓ 二进制识别；大文件 5000 行截断")


def test_html_and_assets():
    code, data = _api_file("page.html")
    assert code == 200 and data["kind"] == "text" and data["ext"] == ".html", data

    for name, expected in (
        ("style.css", "text/css"),
        ("app.js", "text/javascript"),
        ("pic.png", "image/png"),
    ):
        code, ctype, _body = _get(f"/api/workspace/asset/default/{quote(name)}")
        assert code == 200 and ctype.startswith(expected), (name, code, ctype)

    code, _ctype, _body = _get(
        "/api/workspace/asset/default/%2e%2e/ma-wb-secret.txt"
    )
    assert code == 403, code
    code, _ctype, _body = _get(
        "/api/workspace/asset/default/.memory/project.md"
    )
    assert code == 403, code
    code, _ctype, _body = _get("/api/workspace/asset/default/nope.css")
    assert code == 404, code
    print("✓ HTML 文件按文本返回；资源接口 MIME / 越界 / 保护区校验正确")


def test_mhtml():
    for name in ("page.mht", "page.mhtml"):
        code, data = _api_file(name)
        assert code == 200 and data["kind"] == "mhtml", (name, data)
        assert "<img" in data["html"], data["html"]
        for key in ("cid:img1@test", "pic.png", "style.css"):
            assert key in data["resources"], (name, data["resources"].keys())
            assert data["resources"][key].startswith("data:"), data["resources"][key][:30]

    code, data = _api_file("nohtml.mht")
    assert code == 200 and data["kind"] == "unsupported", data
    print("✓ MHTML：提取主 HTML 与内嵌资源；无 HTML 主体时降级为 unsupported")


def test_errors():
    code, _ = _api_file("nope.txt")
    assert code == 404, code
    code, _ctype, _body = _get(
        f"/api/workspace/raw?workspace_id=default&path={quote('a.py')}"
    )
    assert code == 403, code
    print("✓ 缺失文件 404；非图片 raw 403")


def main() -> int:
    print("项目根:", HERE)
    print("---")
    ws = _make_workspace()
    original = workspace_config.resolve_path
    workspace_config.resolve_path = lambda _wid: ws  # type: ignore[assignment]

    srv = WebServer(host="127.0.0.1", port=PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/state", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        workspace_config.resolve_path = original  # type: ignore[assignment]
        shutil.rmtree(ws, ignore_errors=True)
        try:
            os.remove(os.path.join(os.path.dirname(ws), "ma-wb-secret.txt"))
        except OSError:
            pass
        raise RuntimeError("测试服务未启动")

    try:
        test_root_listing()
        test_subdir_and_nav()
        test_path_security()
        test_text_and_encoding()
        test_markdown_and_image()
        test_binary_and_truncation()
        test_html_and_assets()
        test_mhtml()
        test_errors()
    finally:
        if srv._httpd is not None:
            srv._httpd.shutdown()
        workspace_config.resolve_path = original  # type: ignore[assignment]
        shutil.rmtree(ws, ignore_errors=True)
        try:
            os.remove(os.path.join(os.path.dirname(ws), "ma-wb-secret.txt"))
        except OSError:
            pass

    print("---")
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
