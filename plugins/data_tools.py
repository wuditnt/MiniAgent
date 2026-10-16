"""
结构化数据与文件元信息工具组。

和 fs_tools 的分工：
  · fs_tools 管「文件本身」（读原始字节 / 写原始字节 / 列目录 / 找文件）
  · data_tools 管「文件里的结构化内容」（CSV 表格 / JSON / JSONL）
                  以及文件的元信息（大小 / 行数 / 编码 / 修改时间）

所有读取都先走 safe_path 沙箱；超大文件按上限截断，避免刷爆上下文。
"""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import time
from typing import Any

from core import (
    clip,
    decode_bytes,
    looks_binary,
    read_bytes,
    safe_path,
    tool,
    workspace,
)

PLUGIN_INFO = {"name": "结构化数据", "version": "1.0", "author": "mini-agent"}

# 单次读取的最大行数，防止 CSV 几百万行直接吃满上下文
MAX_ROWS = 200


@tool(
    "read_csv",
    "读取 CSV 文件并以 Markdown 表格返回。支持自动分隔符探测；超出上限行会提示。",
    params={
        "path": {"type": "string", "description": "CSV 文件路径"},
        "max_rows": {"type": "integer", "description": "最多显示行数", "default": 50},
        "delimiter": {"type": "string", "description": "分隔符，留空自动探测（逗号/制表符/分号）", "default": ""},
        "encoding": {"type": "string", "description": "编码，留空自动探测", "default": ""},
    },
    required=["path"],
)
def read_csv(path: str, max_rows: int = 50, delimiter: str = "", encoding: str = "") -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    if looks_binary(read_bytes(ap, 4096)):
        return f"[error] {ap} 看起来是二进制文件，不是 CSV"

    data = read_bytes(ap)
    if encoding:
        try:
            text = data.decode(encoding)
            enc = encoding
        except (UnicodeDecodeError, LookupError) as e:
            return f"[error] 用指定编码 {encoding!r} 解码失败：{e}"
    else:
        text, enc = decode_bytes(data)

    # 自动探测分隔符：用 csv.Sniffer，失败则回退到常见三种
    sample = text[:8192]
    delim = delimiter
    if not delim:
        try:
            delim = csv.Sniffer().sniff(sample).delimiter
        except csv.Error:
            for c in (",", "\t", ";"):
                if c in sample:
                    delim = c
                    break
            else:
                delim = ","

    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return f"[ok] {ap} 编码={enc} 分隔符={delim!r} 空文件"

    ncols = max(len(r) for r in rows)
    shown = min(len(rows), max(1, max_rows))
    head = rows[0]
    # 表头 + 分隔行 + 数据行
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] * ncols) + "|"]
    for r in rows[1:shown]:
        cells = [(c or "")[:80].replace("\n", " ") for c in r]
        out.append("| " + " | ".join(cells) + " |")

    truncated = len(rows) - shown
    tail = f"\n…（还有 {truncated} 行未显示，共 {len(rows) - 1} 行数据）" if truncated > 0 else ""
    return f"[{ap}] 编码={enc} 分隔符={delim!r} 共 {len(rows) - 1} 行数据 / {ncols} 列\n" + "\n".join(out) + tail


@tool(
    "read_json",
    "读取 JSON 文件并格式化返回。支持 JSON5 风格的注释（用 run_python 处理更复杂的）。",
    params={
        "path": {"type": "string", "description": "JSON 文件路径"},
        "max_chars": {"type": "integer", "description": "返回最大字符数", "default": 8000},
    },
    required=["path"],
)
def read_json(path: str, max_chars: int = 8000) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    text, enc = decode_bytes(read_bytes(ap))
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return f"[error] JSON 解析失败（编码={enc}）：{e}"
    pretty = json.dumps(obj, ensure_ascii=False, indent=2)
    return clip(pretty, max_chars)


@tool(
    "read_jsonl",
    "读取 JSONL（每行一个 JSON 对象）文件，返回前若干条的格式化结果。",
    params={
        "path": {"type": "string", "description": "JSONL 文件路径"},
        "max_lines": {"type": "integer", "description": "最多读取行数", "default": 20},
    },
    required=["path"],
)
def read_jsonl(path: str, max_lines: int = 20) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    text, enc = decode_bytes(read_bytes(ap))
    lines = text.splitlines()
    out = []
    errors = 0
    for i, line in enumerate(lines[:max(1, max_lines)]):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            out.append(f"[{i + 1}] {json.dumps(obj, ensure_ascii=False)}")
        except json.JSONDecodeError as e:
            errors += 1
            out.append(f"[{i + 1}] [parse error] {e}")
    extra = len(lines) - len(out) - errors
    tail = f"\n…（还有 {extra} 行未显示）" if extra > 0 else ""
    return f"[{ap}] 编码={enc} 共 {len(lines)} 行，显示 {len(out)} 行\n" + "\n".join(out) + tail


@tool(
    "file_info",
    "查看文件的元信息：大小、行数、编码、修改时间、推测的 MIME 类型。",
    params={"path": {"type": "string", "description": "文件路径"}},
    required=["path"],
)
def file_info(path: str) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    st = os.stat(ap)
    size = st.st_size
    mime = mimetypes.guess_type(ap)[0] or "(未知)"
    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))

    if looks_binary(read_bytes(ap, 4096)):
        return f"[{ap}]\n  类型: 二进制（MIME: {mime}）\n  大小: {size:,} 字节\n  修改: {mtime}"

    text, enc = decode_bytes(read_bytes(ap))
    lines = len(text.splitlines())
    return (
        f"[{ap}]\n"
        f"  类型: 文本（MIME: {mime}）\n"
        f"  大小: {size:,} 字节\n"
        f"  编码: {enc}\n"
        f"  行数: {lines:,}\n"
        f"  修改: {mtime}"
    )


@tool(
    "detect_encoding",
    "检测文件的文本编码。返回候选编码列表（按置信度）。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "max_bytes": {"type": "integer", "description": "采样字节数", "default": 65536},
    },
    required=["path"],
)
def detect_encoding(path: str, max_bytes: int = 65536) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    data = read_bytes(ap, max_bytes)

    if looks_binary(data):
        return f"[{ap}] 二进制文件，无文本编码概念"

    # BOM 优先
    if data.startswith(b"\xef\xbb\xbf"):
        return f"[{ap}] UTF-8 BOM"
    if data.startswith(b"\xff\xfe"):
        return f"[{ap}] UTF-16 LE"
    if data.startswith(b"\xfe\xff"):
        return f"[{ap}] UTF-16 BE"

    candidates = []
    for enc in ("utf-8", "gb18030", "gbk", "big5", "latin-1", "cp1252"):
        try:
            data.decode(enc)
            candidates.append(enc)
        except UnicodeDecodeError:
            continue
    if candidates:
        return f"[{ap}] 候选编码（按优先级）: {', '.join(candidates)}"
    return f"[{ap}] 未能可靠识别编码，建议用 run_python 调用 chardet"
