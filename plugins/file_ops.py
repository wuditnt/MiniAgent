"""
文件操作工具组。

和 fs_tools 的分工：
  · fs_tools 管「读」（read_file / list_dir / grep / glob）
  · file_ops 管「改」（写、复制、移动、删除、重命名、追加、对比）

破坏性操作（move / delete）标 dangerous=True，控制台 approve 模式下会先问用户。
"""

from __future__ import annotations

import difflib
import os
import shutil

from core import read_path, safe_path, tool, workspace

PLUGIN_INFO = {"name": "文件操作", "version": "1.0", "author": "mini-agent"}


@tool(
    "copy_file",
    "复制文件或目录到目标位置。目标是已有目录时复制进去。",
    params={
        "src": {"type": "string", "description": "源路径"},
        "dst": {"type": "string", "description": "目标路径"},
    },
    required=["src", "dst"],
)
def copy_file(src: str, dst: str) -> str:
    # 源只读：允许按读取策略从沙箱外复制进来；目标严格：只能写工作区内
    sp = read_path(src)
    dp = safe_path(dst)
    if not os.path.exists(sp):
        return f"[error] 源不存在：{sp}"
    os.makedirs(os.path.dirname(dp) or workspace(), exist_ok=True)
    try:
        if os.path.isdir(sp):
            shutil.copytree(sp, dp, dirs_exist_ok=True)
        else:
            shutil.copy2(sp, dp)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"[ok] 已复制 {sp} → {dp}"


@tool(
    "move_file",
    "移动文件或目录（会删除源位置）。目标是已有目录时移动进去。",
    params={
        "src": {"type": "string", "description": "源路径"},
        "dst": {"type": "string", "description": "目标路径"},
    },
    required=["src", "dst"],
    dangerous=True,
)
def move_file(src: str, dst: str) -> str:
    sp = safe_path(src)
    dp = safe_path(dst)
    if not os.path.exists(sp):
        return f"[error] 源不存在：{sp}"
    os.makedirs(os.path.dirname(dp) or workspace(), exist_ok=True)
    try:
        shutil.move(sp, dp)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"[ok] 已移动 {sp} → {dp}"


@tool(
    "delete_file",
    "删除文件或目录（目录会递归删除）。此操作不可撤销。",
    params={"path": {"type": "string", "description": "要删除的路径"}},
    required=["path"],
    dangerous=True,
)
def delete_file(path: str) -> str:
    ap = safe_path(path)
    if not os.path.exists(ap):
        return f"[error] 路径不存在：{ap}"
    try:
        if os.path.isdir(ap):
            shutil.rmtree(ap)
        else:
            os.remove(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"[ok] 已删除 {ap}"


@tool(
    "make_dir",
    "创建目录（以及不存在的父目录）。",
    params={"path": {"type": "string", "description": "目录路径"}},
    required=["path"],
)
def make_dir(path: str) -> str:
    ap = safe_path(path)
    os.makedirs(ap, exist_ok=True)
    return f"[ok] 目录已就绪：{ap}"


@tool(
    "rename",
    "重命名文件或目录（在同一目录内）。",
    params={
        "path": {"type": "string", "description": "原路径"},
        "new_name": {"type": "string", "description": "新名字（不含目录）"},
    },
    required=["path", "new_name"],
)
def rename(path: str, new_name: str) -> str:
    ap = safe_path(path)
    if not os.path.exists(ap):
        return f"[error] 路径不存在：{ap}"
    if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        return f"[error] new_name 只能是单个文件名：{new_name!r}"
    dst = os.path.join(os.path.dirname(ap) or workspace(), new_name)
    try:
        os.rename(ap, dst)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
    return f"[ok] 已重命名 {ap} → {dst}"


@tool(
    "append_to_file",
    "在文件末尾追加文本（不存在则创建）。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "content": {"type": "string", "description": "要追加的内容"},
    },
    required=["path", "content"],
)
def append_to_file(path: str, content: str) -> str:
    ap = safe_path(path)
    os.makedirs(os.path.dirname(ap) or workspace(), exist_ok=True)
    with open(ap, "a", encoding="utf-8") as f:
        f.write(content)
    return f"[ok] 已追加 {len(content)} 字节到 {ap}"


@tool(
    "diff_files",
    "对比两个文本文件，返回 unified diff 格式的差异。",
    params={
        "path_a": {"type": "string", "description": "文件 A（旧）"},
        "path_b": {"type": "string", "description": "文件 B（新）"},
        "context": {"type": "integer", "description": "差异上下文行数", "default": 3},
    },
    required=["path_a", "path_b"],
)
def diff_files(path_a: str, path_b: str, context: int = 3) -> str:
    from core import decode_bytes, looks_binary, read_bytes

    for label, p in (("A", path_a), ("B", path_b)):
        if not os.path.isfile(read_path(p)):
            return f"[error] 文件 {label} 不存在：{p}"

    data_a = read_bytes(read_path(path_a))
    data_b = read_bytes(read_path(path_b))
    if looks_binary(data_a) or looks_binary(data_b):
        return "[error] 二进制文件不支持文本 diff"

    ta = decode_bytes(data_a)[0].splitlines(keepends=True)
    tb = decode_bytes(data_b)[0].splitlines(keepends=True)
    diff = difflib.unified_diff(ta, tb, fromfile=path_a, tofile=path_b, n=max(0, context))
    out = "".join(diff)
    if not out:
        return f"[ok] 两个文件内容完全相同"
    from core import clip

    return clip(out)
