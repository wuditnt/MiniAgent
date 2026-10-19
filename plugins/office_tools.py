# pip install python-docx openpyxl pypdf pdfplumber
"""
办公文档工具组：Word(.docx) / Excel(.xlsx) / PDF。

和邻居插件的分工：
  · data_tools   管「纯文本里的结构化内容」（CSV / JSON / JSONL）
  · office_tools 管「二进制文档容器」（docx / xlsx / pdf）
  · archive_tools 管压缩包本身，不解析内部内容

设计取舍：
  · 第三方库一律**懒加载**（在函数体内 import）：没装时只让这一个工具
    返回可读提示，而不是让整个插件加载失败。装了哪个库就多哪项能力。
  · 读走 read_path、写走 safe_path，和其他插件一样受路径沙箱约束。
  · 输出统一 clip 截断，避免把上下文刷爆（表格与页数都带上限参数）。
"""

from __future__ import annotations

import os

from core import (
    clip,
    read_path,
    safe_path,
    tool,
    workspace,
)

PLUGIN_INFO = {
    "name": "办公文档",
    "version": "1.0",
    "author": "mini-agent",
    "desc": "读写 Word/Excel、读取 PDF（依赖 python-docx / openpyxl / pypdf / pdfplumber）",
}

# 单次返回的默认字符上限
DEFAULT_MAX_CHARS = 8000


def _need(lib: str, pip_name: str = "") -> str:
    """统一的「缺第三方库」提示。"""
    return (
        f"[error] 需要第三方库 {lib}，当前环境未安装。\n"
        f"  安装：python -m pip install {pip_name or lib}\n"
        f"  （装到本项目虚拟环境，勿用系统 Python）"
    )


def _parse_pages(spec: str, total: int, max_pages: int) -> list[int]:
    """把 "1-3,5" 解析成 0 起始的页索引；留空则取前 max_pages 页。

    超出范围 / 解析失败的片段直接忽略；全空时回退到最前面几页。
    """
    limit = max(1, max_pages)
    spec = (spec or "").strip()
    if not spec:
        return list(range(min(total, limit)))

    picked: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
        else:
            try:
                start = end = int(part)
            except ValueError:
                continue
        for p in range(start, end + 1):
            if 1 <= p <= total and (p - 1) not in picked:
                picked.append(p - 1)

    return picked[:limit] or list(range(min(total, limit)))


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


# ----------------------------------------------------------------- Excel


@tool(
    "read_xlsx",
    "读取 Excel(.xlsx) 工作表，以 Markdown 表格返回。不给 sheet 时列出全部表名并展示第一张。"
    "依赖第三方库 openpyxl。",
    params={
        "path": {"type": "string", "description": "xlsx 文件路径"},
        "sheet": {"type": "string", "description": "工作表名，留空取第一张", "default": ""},
        "max_rows": {"type": "integer", "description": "最多显示行数", "default": 50},
        "max_cols": {"type": "integer", "description": "最多显示列数", "default": 30},
    },
    required=["path"],
)
def read_xlsx(path: str, sheet: str = "", max_rows: int = 50, max_cols: int = 30) -> str:
    try:
        import openpyxl
    except ImportError:
        return _need("openpyxl")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        wb = openpyxl.load_workbook(ap, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        return f"[error] 解析 .xlsx 失败（{type(e).__name__}: {e}）"

    names = list(wb.sheetnames)
    if not names:
        return f"[error] {ap} 没有任何工作表"
    target = sheet if sheet in names else names[0]

    ws = wb[target]
    nrows = ws.max_row or 0
    ncols = ws.max_column or 0
    shown_rows = min(nrows, max(1, max_rows))
    shown_cols = min(ncols, max(1, max_cols))

    data = [
        ["" if v is None else str(v) for v in r]
        for r in ws.iter_rows(min_row=1, max_row=shown_rows, max_col=shown_cols, values_only=True)
    ]
    wb.close()

    head = f"[{ap}] 工作表 {names} / 当前: {target} / {nrows} 行 × {ncols} 列"
    if sheet and sheet != target:
        head += f"（未找到表 {sheet!r}，已回退到 {target}）"
    if nrows > shown_rows or ncols > shown_cols:
        head += f"\n…（仅显示前 {shown_rows} 行 × {shown_cols} 列）"

    table = _md_table(data) if data else "（空表）"
    return f"{head}\n{table}"


@tool(
    "write_xlsx",
    "把二维数组写成 Excel(.xlsx)。rows 是行数组，每行是单元格数组。"
    "依赖第三方库 openpyxl。",
    params={
        "path": {"type": "string", "description": "要写入的 xlsx 路径"},
        "rows": {"type": "array", "items": {"type": "array"},
                 "description": '二维数组，如 [["姓名","年龄"],["张三",18]]'},
        "sheet": {"type": "string", "description": "工作表名", "default": "Sheet1"},
    },
    required=["path", "rows"],
)
def write_xlsx(path: str, rows: list, sheet: str = "Sheet1") -> str:
    try:
        import openpyxl
    except ImportError:
        return _need("openpyxl")

    ap = safe_path(path)
    os.makedirs(os.path.dirname(ap) or workspace(), exist_ok=True)

    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet or "Sheet1"
        for r in rows or []:
            ws.append(list(r) if isinstance(r, (list, tuple)) else [r])
        wb.save(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 生成 .xlsx 失败（{type(e).__name__}: {e}）"

    return f"[ok] 已写入 {ap}（表 {ws.title}，{ws.max_row} 行 × {ws.max_column} 列，{os.path.getsize(ap)} 字节）"


# ------------------------------------------------------------------- PDF


@tool(
    "read_pdf",
    "提取 PDF 的文字内容（按页）。扫描件/纯图片 PDF 提不出文字，需要 OCR 另行处理。"
    "依赖第三方库 pypdf。",
    params={
        "path": {"type": "string", "description": "pdf 文件路径"},
        "pages": {"type": "string", "description": '页号，如 "1-3,5"；留空取前若干页', "default": ""},
        "max_pages": {"type": "integer", "description": "最多读取页数", "default": 5},
        "max_chars": {"type": "integer", "description": "返回最大字符数", "default": 8000},
    },
    required=["path"],
)
def read_pdf(path: str, pages: str = "", max_pages: int = 5, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return _need("pypdf")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        reader = PdfReader(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 解析 PDF 失败（{type(e).__name__}: {e}）"

    total = len(reader.pages)
    idxs = _parse_pages(pages, total, max_pages)

    chunks: list[str] = []
    for i in idxs:
        try:
            text = (reader.pages[i].extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001
            text = f"[该页提取失败: {type(e).__name__}: {e}]"
        chunks.append(f"--- 第 {i + 1} 页 ---\n{text or '(本页无文字)'}")

    body = "\n\n".join(chunks)
    head = f"[{ap}] 共 {total} 页，提取第 {idxs[0] + 1}-{idxs[-1] + 1} 页"
    if not body.strip():
        return f"{head}\n（未提取到任何文字，可能是扫描件，需 OCR）"
    return clip(f"{head}\n{body}", max_chars)


@tool(
    "read_pdf_tables",
    "用 pdfplumber 提取 PDF 里的表格并转成 Markdown。适合表格型 PDF，扫描件无效。"
    "依赖第三方库 pdfplumber。",
    params={
        "path": {"type": "string", "description": "pdf 文件路径"},
        "pages": {"type": "string", "description": '页号，如 "1-3"；留空取前 3 页', "default": ""},
        "max_tables": {"type": "integer", "description": "最多提取表格数", "default": 5},
        "max_rows": {"type": "integer", "description": "每个表格最多显示行数", "default": 50},
    },
    required=["path"],
)
def read_pdf_tables(path: str, pages: str = "", max_tables: int = 5, max_rows: int = 50) -> str:
    try:
        import pdfplumber
    except ImportError:
        return _need("pdfplumber")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    out: list[str] = []
    try:
        with pdfplumber.open(ap) as pdf:
            total = len(pdf.pages)
            idxs = _parse_pages(pages, total, 3)
            count = 0
            for i in idxs:
                if count >= max(1, max_tables):
                    break
                for tbl in pdf.pages[i].extract_tables() or []:
                    if count >= max(1, max_tables):
                        break
                    count += 1
                    rows = [["" if c is None else str(c).strip() for c in r] for r in tbl[: max(1, max_rows)]]
                    out.append(f"**第 {i + 1} 页 · 表 {count}**")
                    out.append(_md_table(rows))
    except Exception as e:  # noqa: BLE001
        return f"[error] 提取 PDF 表格失败（{type(e).__name__}: {e}）"

    head = f"[{ap}] 共 {total} 页，提取到 {len(out) // 2} 个表格"
    if not out:
        return f"{head}\n（未识别到表格；可先用 read_pdf 看文字，扫描件需 OCR）"
    return clip(head + "\n" + "\n".join(out))
