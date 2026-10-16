"""
代码辅助工具组。

py_syntax_check  检查 Python 语法（不用真的跑，AST 解析即可）
count_code_lines  按目录统计代码行数（按扩展名分组，自动跳过空行/注释）
re_replace        用正则替换文件里的内容（批量改代码时很好用）
"""

from __future__ import annotations

import ast
import os
import re
import sys

from core import decode_bytes, looks_binary, read_bytes, safe_path, tool

PLUGIN_INFO = {"name": "代码辅助", "version": "1.0", "author": "mini-agent"}


@tool(
    "py_syntax_check",
    "检查 Python 文件的语法是否合法，返回错误行号或「语法 OK」。不执行代码。",
    params={"path": {"type": "string", "description": "Python 文件路径"}},
    required=["path"],
)
def py_syntax_check(path: str) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    if looks_binary(read_bytes(ap, 4096)):
        return f"[error] {ap} 不是文本文件"
    text, enc = decode_bytes(read_bytes(ap))
    try:
        ast.parse(text, filename=ap)
    except SyntaxError as e:
        return f"[syntax error] {ap}\n  行 {e.lineno} 列 {e.offset}: {e.msg}"
    return f"[ok] {ap} 语法正确（编码={enc}，{len(text.splitlines())} 行）"


@tool(
    "count_code_lines",
    "统计目录下各语言的代码行数（自动跳过空行和注释）。",
    params={
        "path": {"type": "string", "description": "目录路径，留空统计工作区", "default": "."},
        "max_files": {"type": "integer", "description": "最多扫描文件数", "default": 2000},
    },
)
def count_code_lines(path: str = ".", max_files: int = 2000) -> str:
    import glob as _glob

    ap = safe_path(path)
    if not os.path.isdir(ap):
        return f"[error] 目录不存在：{ap}"

    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".java": "Java", ".go": "Go", ".rs": "Rust", ".c": "C", ".cpp": "C++",
        ".h": "C/C++ Header", ".html": "HTML", ".css": "CSS", ".md": "Markdown",
        ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
        ".sh": "Shell", ".bat": "Batch", ".ps1": "PowerShell",
    }

    stats: dict[str, dict[str, int]] = {}
    scanned = 0
    for fp in _glob.glob(os.path.join(ap, "**", "*"), recursive=True):
        if scanned >= max_files:
            break
        if not os.path.isfile(fp):
            continue
        ext = os.path.splitext(fp)[1].lower()
        lang = ext_map.get(ext)
        if lang is None:
            continue
        if looks_binary(read_bytes(fp, 4096)):
            continue
        scanned += 1
        text, _ = decode_bytes(read_bytes(fp))
        total = len(text.splitlines())
        blank = sum(1 for l in text.splitlines() if not l.strip())
        comment = sum(
            1 for l in text.splitlines()
            if l.strip().startswith(("#", "//", "/*", "*", "--", ";"))
        )
        code = total - blank - comment
        s = stats.setdefault(lang, {"files": 0, "total": 0, "code": 0, "blank": 0, "comment": 0})
        s["files"] += 1
        s["total"] += total
        s["code"] += max(0, code)
        s["blank"] += blank
        s["comment"] += comment

    if not stats:
        return f"[{ap}] 没有找到可统计的代码文件"

    rows = sorted(stats.items(), key=lambda kv: kv[1]["code"], reverse=True)
    lines = [f"[{ap}] 扫描 {scanned} 个文件\n", "语言        文件数  总行  代码  空行  注释"]
    for lang, s in rows:
        lines.append(
            f"{lang:10s} {s['files']:5d} {s['total']:6d} {s['code']:6d} {s['blank']:5d} {s['comment']:6d}"
        )
    total_code = sum(s["code"] for s in stats.values())
    lines.append(f"\n代码行合计: {total_code:,}")
    return "\n".join(lines)


@tool(
    "re_replace",
    "用正则表达式替换文件里的内容。返回替换了多少处，以及前几处的前后对比。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "pattern": {"type": "string", "description": "要查找的正则表达式"},
        "repl": {"type": "string", "description": "替换内容（支持 \\1 等反向引用）"},
        "count": {"type": "integer", "description": "最多替换处数，0 表示全部", "default": 0},
    },
    required=["path", "pattern", "repl"],
    dangerous=True,
)
def re_replace(path: str, pattern: str, repl: str, count: int = 0) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[error] 正则不合法：{e}"

    data = read_bytes(ap)
    if looks_binary(data):
        return f"[error] 不支持对二进制文件做文本替换：{ap}"
    text, enc = decode_bytes(data)

    # 先预览前 3 处
    previews = []
    for m in rx.finditer(text):
        if len(previews) >= 3:
            break
        start = max(0, m.start() - 20)
        end = min(len(text), m.end() + 20)
        original = text[start:end].replace("\n", " ")
        replaced = rx.sub(repl, text[start:end], count=1)
        previews.append(f"  - {original!r} → {replaced!r}")

    new_text, n = rx.subn(repl, text, count=count if count else 0)
    if n == 0:
        return f"[ok] 没有匹配到 {pattern!r}，文件未改动"

    with open(ap, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    preview = "\n" + "\n".join(previews) if previews else ""
    return f"[ok] {ap} 替换 {n} 处（编码={enc}）{preview}"
