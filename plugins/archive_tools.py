"""
压缩归档工具组。

全用 stdlib（zipfile / tarfile），不依赖外部命令。
支持 .zip / .tar / .tar.gz / .tar.bz2 / .tar.xz 自动识别。
"""

from __future__ import annotations

import os
import tarfile
import zipfile

from core import safe_path, tool, workspace

PLUGIN_INFO = {"name": "压缩归档", "version": "1.0", "author": "mini-agent"}


def _detect_kind(path: str) -> str:
    low = path.lower()
    if low.endswith(".zip"):
        return "zip"
    for suf in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar"):
        if low.endswith(suf):
            return "tar"
    return "zip"  # 默认按 zip 处理


@tool(
    "zip_create",
    "把文件或目录打包成压缩包。格式按输出文件扩展名自动判断（.zip / .tar.gz 等）。",
    params={
        "paths": {"type": "array", "items": {"type": "string"},
                  "description": "要打包的文件/目录路径列表"},
        "archive": {"type": "string", "description": "输出压缩包路径（如 output.zip）"},
    },
    required=["paths", "archive"],
    dangerous=True,
)
def zip_create(paths: list[str], archive: str) -> str:
    ap = safe_path(archive)
    os.makedirs(os.path.dirname(ap) or workspace(), exist_ok=True)
    kind = _detect_kind(ap)

    added = []
    try:
        if kind == "zip":
            with zipfile.ZipFile(ap, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    src = safe_path(p)
                    if os.path.isdir(src):
                        for root, _dirs, files in os.walk(src):
                            for f in files:
                                full = os.path.join(root, f)
                                arc = os.path.relpath(full, os.path.dirname(src))
                                zf.write(full, arc)
                                added.append(arc)
                    elif os.path.isfile(src):
                        zf.write(src, os.path.basename(src))
                        added.append(os.path.basename(src))
        else:
            mode = "w:gz" if ap.lower().endswith((".tar.gz", ".tgz")) else \
                   "w:bz2" if ap.lower().endswith((".tar.bz2", ".tbz2")) else \
                   "w:xz" if ap.lower().endswith((".tar.xz", ".txz")) else "w"
            with tarfile.open(ap, mode) as tf:
                for p in paths:
                    src = safe_path(p)
                    tf.add(src, arcname=os.path.basename(src))
                    added.append(os.path.basename(src))
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"[ok] 已创建 {ap}，共 {len(added)} 个条目\n" + "\n".join(f"  + {a}" for a in added)


@tool(
    "zip_extract",
    "解压压缩包到指定目录。",
    params={
        "archive": {"type": "string", "description": "压缩包路径"},
        "path": {"type": "string", "description": "解压目标目录，留空用压缩包同名目录", "default": ""},
    },
    required=["archive"],
    dangerous=True,
)
def zip_extract(archive: str, path: str = "") -> str:
    ap = safe_path(archive)
    if not os.path.isfile(ap):
        return f"[error] 压缩包不存在：{ap}"
    kind = _detect_kind(ap)
    target = safe_path(path) if path else os.path.splitext(ap)[0]
    os.makedirs(target, exist_ok=True)

    try:
        if kind == "zip":
            with zipfile.ZipFile(ap) as zf:
                names = zf.namelist()
                zf.extractall(target)
        else:
            with tarfile.open(ap) as tf:
                names = tf.getnames()
                tf.extractall(target)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"[ok] 已解压 {ap} → {target}（{len(names)} 个条目）"


@tool(
    "list_archive",
    "列出压缩包内的文件清单（不解压）。",
    params={
        "archive": {"type": "string", "description": "压缩包路径"},
        "max_items": {"type": "integer", "description": "最多显示条数", "default": 200},
    },
    required=["archive"],
)
def list_archive(archive: str, max_items: int = 200) -> str:
    ap = safe_path(archive)
    if not os.path.isfile(ap):
        return f"[error] 压缩包不存在：{ap}"
    kind = _detect_kind(ap)
    try:
        if kind == "zip":
            with zipfile.ZipFile(ap) as zf:
                infos = zf.infolist()
                rows = [f"  {i.file_size:>12,}  {i.filename}" for i in infos[:max_items]]
                total = len(infos)
        else:
            with tarfile.open(ap) as tf:
                members = tf.getmembers()
                rows = [f"  {m.size:>12,}  {m.name}" for m in members[:max_items]]
                total = len(members)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    more = total - len(rows)
    tail = f"\n…（还有 {more} 条未显示）" if more > 0 else ""
    return f"[{ap}] 共 {total} 个条目\n" + "\n".join(rows) + tail
