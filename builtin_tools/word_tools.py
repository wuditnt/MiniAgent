# pip install python-docx mammoth markdownify
"""
Word 文档工具组（.docx）：读取、写入、高保真转 Markdown。

独立边界：
  · 只处理 .docx；.pdf 见 pdf_tools，.xlsx 见 sheet_tools，.pptx 见 pptx_tools。
  · 不与任何兄弟模块互相 import；公共能力只依赖 core（clip / read_path / safe_path / tool）。
  · 第三方库懒加载：缺库时只让当前工具返回可读提示，不影响其它工具加载。
"""

from __future__ import annotations

import os

from core import clip, read_path, safe_path, tool, workspace

TOOL_INFO = {
    "name": "Word 文档",
    "version": "1.0",
    "author": "mini-agent",
    "desc": "读写 Word(.docx)、docx→Markdown 高保真（mammoth→HTML→markdownify）",
}

DEFAULT_MAX_CHARS = 8000


def _need(lib: str, pip_name: str = "") -> str:
    """统一的「缺第三方库」提示。"""
    return (
        f"[error] 需要第三方库 {lib}，当前环境未安装。\n"
        f"  安装：python -m pip install {pip_name or lib}\n"
        f"  （装到本项目虚拟环境，勿用系统 Python）"
    )


def _md_table(rows: list[list[str]]) -> str:
    """把二维数据渲染成 Markdown 表格（行宽按最长行对齐）。"""
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    head = rows[0] + [""] * (ncols - len(rows[0]))
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * ncols) + "|"]
    for r in rows[1:]:
        cells = [c for c in r] + [""] * (ncols - len(r))
        out.append("| " + " | ".join(c[:80].replace("\n", " ") for c in cells) + " |")
    return "\n".join(out)


def _is_sep_row(cells: list[str]) -> bool:
    """Markdown 表格的 |---|---| 分隔行。"""
    return all(c and set(c) <= set("-: ") for c in cells)


# ------------------------------------------------------------------ Word


@tool(
    "read_docx",
    "读取 Word(.docx)：按标题层级转成 Markdown，段落、列表、表格都会保留。"
    "依赖第三方库 python-docx。",
    group="文档·Word",
    params={
        "path": {"type": "string", "description": "docx 文件路径"},
        "max_chars": {"type": "integer", "description": "返回最大字符数", "default": 8000},
        "include_tables": {"type": "boolean", "description": "是否把表格转为 Markdown 表格", "default": True},
    },
    required=["path"],
)
def read_docx(path: str, max_chars: int = DEFAULT_MAX_CHARS, include_tables: bool = True) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        return _need("python-docx")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        document = docx.Document(ap)
    except Exception as e:  # noqa: BLE001  —— 坏文件的报错信息对模型没意义，给类型就够
        return f"[error] 解析 .docx 失败（{type(e).__name__}: {e}）"

    out: list[str] = []
    for p in document.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style = (p.style.name or "").lower() if p.style else ""
        if style.startswith("heading"):
            level = "".join(c for c in style if c.isdigit()) or "1"
            out.append("#" * min(int(level), 6) + " " + text)
        elif "list" in style:
            out.append("- " + text)
        else:
            out.append(text)

    if include_tables:
        for i, table in enumerate(document.tables, 1):
            rows = [[(c.text or "").strip().replace("\n", " ") for c in r.cells] for r in table.rows]
            if not rows:
                continue
            out.append(f"\n**表 {i}**")
            out.append(_md_table(rows))

    body = "\n".join(out)
    return clip(
        f"[{ap}] 段落 {len(document.paragraphs)} 个 / 表格 {len(document.tables)} 个\n{body}",
        max_chars,
    )


@tool(
    "write_docx",
    "把 Markdown 文本写成 Word(.docx)：支持 # 标题、- 列表、| 表格，其余行按普通段落。"
    "依赖第三方库 python-docx。",
    group="文档·Word",
    params={
        "path": {"type": "string", "description": "要写入的 docx 路径"},
        "content": {"type": "string", "description": "Markdown 正文"},
    },
    required=["path", "content"],
)
def write_docx(path: str, content: str) -> str:
    try:
        import docx
    except ImportError:
        return _need("python-docx")

    ap = safe_path(path)
    os.makedirs(os.path.dirname(ap) or workspace(), exist_ok=True)

    try:
        document = docx.Document()
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue

            # 连续的 | 行视为一个表格块
            if stripped.startswith("|"):
                block: list[list[str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    if not _is_sep_row(cells):
                        block.append(cells)
                    i += 1
                if block:
                    ncols = max(len(r) for r in block)
                    table = document.add_table(rows=len(block), cols=ncols)
                    table.style = "Table Grid"
                    for ri, r in enumerate(block):
                        for ci in range(ncols):
                            table.cell(ri, ci).text = r[ci] if ci < len(r) else ""
                continue

            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                document.add_heading(stripped[level:].strip(), level=min(level, 6))
            elif stripped.startswith(("- ", "* ")):
                document.add_paragraph(stripped[2:].strip(), style="List Bullet")
            else:
                document.add_paragraph(stripped)
            i += 1

        document.save(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 生成 .docx 失败（{type(e).__name__}: {e}）"

    return f"[ok] 已写入 {ap}（{os.path.getsize(ap)} 字节）"


def _count_docx_meta(ap: str) -> tuple[int, int]:
    """数一下 docx 里有几个表格、几张图片（用来在转换结果后附一句提示）。"""
    try:
        import docx
    except ImportError:
        return 0, 0
    try:
        d = docx.Document(ap)
        return len(d.tables), len(d.inline_shapes)
    except Exception:  # noqa: BLE001
        return 0, 0


def _docx_html(ap: str, mammoth) -> str:
    """mammoth 解析 docx → HTML（保留标题/列表/加粗斜体/链接/表格等结构）。"""
    with open(ap, "rb") as f:
        return mammoth.convert_to_html(f).value or ""


@tool(
    "docx_to_markdown",
    "把 Word(.docx) 转成 Markdown（高保真）：用 mammoth 把文档解析成 HTML 结构，"
    "再由 markdownify 转成 Markdown —— 标题层级、嵌套列表、加粗/斜体、链接、表格都能尽量保留。"
    "图片不内嵌（Markdown 不写二进制），会略去并提示数量；需要逐段查看正文可用 read_docx。"
    "依赖第三方库 mammoth（表格保真还需 markdownify）。",
    group="文档·Word",
    params={
        "path": {"type": "string", "description": "docx 文件路径"},
        "max_chars": {"type": "integer", "description": "返回最大字符数", "default": 8000},
    },
    required=["path"],
)
def docx_to_markdown(path: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    try:
        import mammoth
    except ImportError:
        return _need("mammoth")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        html = _docx_html(ap, mammoth)
    except Exception as e:  # noqa: BLE001
        return f"[error] 转换 .docx 失败（{type(e).__name__}: {e}）"

    # mammoth 的 HTML 保留全部结构，最关键的表格也是；
    # 再经 markdownify 转 Markdown，就能一次拿到「表格结构 + 行内格式」。
    # markdownify 缺失时退回 mammoth 自带的 Markdown writer（表格会退化为零散段落）。
    try:
        from markdownify import markdownify
    except ImportError:
        try:
            with open(ap, "rb") as f:
                md = (mammoth.convert_to_markdown(f).value or "").strip()
        except Exception as e:  # noqa: BLE001
            return f"[error] 转换 .docx 失败（{type(e).__name__}: {e}）"
        return clip(
            f"[{ap}] 转换完成\n{md}\n"
            "（未安装 markdownify，表格结构可能丢失；"
            "安装 python -m pip install markdownify 可保留表格）",
            max_chars,
        )

    # heading_style="ATX" 让标题输出 `# H1` 而非 Setext 的 `H1\n===`，与本项目其它工具一致；
    # table_infer_header=True 把表格首行提升为表头（mammoth 对普通 Word 表格输出全 <td>，
    #   不推断的话首行会变成空白表头 `|  |  |`）；
    # strip=["img"] 丢弃图片 —— mammoth 默认会把图片转成 base64 data URI，会撑爆输出。
    md = markdownify(
        html, strip=["img"], heading_style="ATX", table_infer_header=True
    ).strip()

    notes = ""
    tables, images = _count_docx_meta(ap)
    if tables:
        notes += f"\n（本文档含 {tables} 个表格，已转为 Markdown 表格）"
    if images:
        notes += f"\n（本文档含 {images} 张图片，Markdown 不内嵌二进制，已略去）"

    return clip(f"[{ap}] 转换完成\n{md}{notes}", max_chars)
