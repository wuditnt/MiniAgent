"""
文件系统工具组。

这是插件写法的参考实现：所有文件操作先过 safe_path（路径沙箱），
返回值一律是字符串（模型只能看到文本）。
"""

from __future__ import annotations

import os

from core import (
    MEMORY_DIRNAME,
    clip,
    decode_bytes,
    hexdump,
    is_memory_path,
    looks_binary,
    read_bytes,
    read_path,
    safe_path,
    tool,
    workspace,
)

PLUGIN_INFO = {"name": "文件系统", "version": "1.0", "author": "mini-agent"}

# 二进制文件的「下一步该用哪个工具」——按扩展名路由到办公文档专用工具。
# 目的是让模型一步到位，不必先读一遍十六进制流才发现该换工具。
_OFFICE_READ_TOOLS = {
    ".docx": "read_docx",
    ".xlsx": "read_xlsx",
    ".xlsm": "read_xlsx",
    ".pdf": "read_pdf（表格型 PDF 用 read_pdf_tables）",
}


def _binary_hint(ap: str) -> str:
    """二进制文件读不进来时，给模型一句明确的下一步指引。"""
    hint = _OFFICE_READ_TOOLS.get(os.path.splitext(ap)[1].lower())
    if hint:
        return f"这是办公文档，请直接改用 {hint} 工具，不要读二进制流。"
    return "如需解析内容，请改用 run_python 传对应库（如 pypdf / openpyxl）处理。"


@tool(
    "read_file",
    "读取文本文件内容，返回带行号的文本。二进制文件（PDF/Office/图片/加密文件等）"
    "不会硬解，只返回十六进制预览并提示改用对应的专用工具。"
    "注意：docx / xlsx / pdf 请直接用 read_docx / read_xlsx / read_pdf，不要先调本工具。",
    params={
        "path": {"type": "string", "description": "文件路径（相对工作区或绝对路径）"},
        "offset": {"type": "integer", "description": "起始行号，从 1 开始", "default": 1},
        "limit": {"type": "integer", "description": "最多读取行数", "default": 300},
    },
    required=["path"],
)
def read_file(path: str, offset: int = 1, limit: int = 300) -> str:
    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    data = read_bytes(ap)

    # 关键降级逻辑：二进制不硬解，给 hex 预览 + 明确的下一步指引
    if looks_binary(data):
        return (
            f"[binary] {ap}（{len(data)} 字节）不是文本文件，已返回十六进制预览。\n"
            f"{_binary_hint(ap)}\n\n"
            f"{hexdump(data)}"
        )

    text, enc = decode_bytes(data)
    lines = text.splitlines()
    start = max(0, offset - 1)
    chunk = lines[start: start + limit]
    body = "\n".join(f"{start + i + 1:6d}\t{ln}" for i, ln in enumerate(chunk))
    return f"[{ap}] 编码={enc} 共 {len(lines)} 行，显示第 {start + 1}-{start + len(chunk)} 行\n{body}"


@tool(
    "write_file",
    "把文本写入文件（覆盖）。父目录不存在会自动创建。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "content": {"type": "string", "description": "要写入的完整内容"},
    },
    required=["path", "content"],
)
def write_file(path: str, content: str) -> str:
    ap = safe_path(path)
    os.makedirs(os.path.dirname(ap) or workspace(), exist_ok=True)
    with open(ap, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"[ok] 已写入 {ap}（{len(content.encode('utf-8'))} 字节）"


@tool(
    "list_dir",
    "列出目录下的文件与子目录。",
    params={"path": {"type": "string", "description": "目录路径", "default": "."}},
)
def list_dir(path: str = ".") -> str:
    ap = read_path(path)
    if not os.path.isdir(ap):
        return f"[error] 目录不存在：{ap}"
    rows = []
    for name in sorted(os.listdir(ap)):
        full = os.path.join(ap, name)
        # .memory 系统保护区对工具完全不可见（read_path 也会拦，列举这里提前过滤）
        if name == MEMORY_DIRNAME and is_memory_path(full):
            continue
        if os.path.isdir(full):
            rows.append(f"  [dir ] {name}/")
        else:
            rows.append(f"  [file] {name}  ({os.path.getsize(full)} B)")
    return f"[{ap}] {len(rows)} 项\n" + "\n".join(rows)


@tool(
    "glob_files",
    "按通配符递归查找文件，例如 **/*.py 。",
    params={
        "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
        "path": {"type": "string", "description": "搜索根目录", "default": "."},
    },
    required=["pattern"],
)
def glob_files(pattern: str, path: str = ".") -> str:
    import glob as _glob
    ap = read_path(path)
    hits = []
    for h in _glob.glob(os.path.join(ap, pattern), recursive=True):
        if not os.path.isfile(h):
            continue
        # .memory 保护区直接跳过（下面的 read_path 也会兜底拒绝）
        if is_memory_path(h):
            continue
        # 再校验一次：pattern 里的 .. 或符号链接都不能把结果带出允许范围
        try:
            read_path(os.path.realpath(h))
        except Exception:
            continue
        hits.append(h)
    if not hits:
        return f"[ok] 无匹配：{pattern}"
    root = workspace()
    labels = []
    for h in hits[:200]:
        rel = os.path.relpath(h, root)
        labels.append("  " + (rel if not rel.startswith("..") else os.path.abspath(h)))
    return f"[ok] {len(hits)} 个匹配\n" + "\n".join(labels)


@tool(
    "grep",
    "在文件中按正则搜索文本，返回 文件:行号:内容。",
    params={
        "pattern": {"type": "string", "description": "正则表达式"},
        "path": {"type": "string", "description": "搜索根目录", "default": "."},
        "glob": {"type": "string", "description": "可选文件过滤，如 *.py", "default": "*"},
    },
    required=["pattern"],
)
def grep(pattern: str, path: str = ".", glob: str = "*") -> str:
    import glob as _glob
    import re

    ap = read_path(path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[error] 正则不合法：{e}"

    root = workspace()
    out: list[str] = []
    scanned = 0
    for fp in _glob.glob(os.path.join(ap, "**", glob), recursive=True):
        if not os.path.isfile(fp):
            continue
        if is_memory_path(fp):
            continue
        # 防 pattern 内 .. / 符号链接把读取带出允许范围
        try:
            read_path(os.path.realpath(fp))
        except Exception:
            continue
        try:
            data = read_bytes(fp, 2 * 1024 * 1024)
        except OSError:
            continue
        if looks_binary(data):
            continue
        scanned += 1
        text, _ = decode_bytes(data)
        label = os.path.relpath(fp, root)
        if label.startswith(".."):
            label = os.path.abspath(fp)   # 沙箱外文件显示绝对路径
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{label}:{i}:{line.strip()[:200]}")
                if len(out) >= 200:
                    break
        if len(out) >= 200:
            break
    header = f"[ok] 扫描 {scanned} 个文本文件，命中 {len(out)} 行（上限 200）"
    return header + ("\n" + "\n".join(out) if out else "")
