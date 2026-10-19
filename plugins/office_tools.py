# pip install python-docx openpyxl pypdf pdfplumber python-calamine reportlab mammoth markdownify
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
import re

from core import (
    clip,
    read_path,
    safe_path,
    tool,
    workspace,
)

PLUGIN_INFO = {
    "name": "办公文档",
    "version": "1.1",
    "author": "mini-agent",
    "desc": "读写 Word/Excel/PDF、docx→Markdown 高保真（mammoth→HTML→markdownify）"
            "（依赖 python-docx / openpyxl / pypdf / pdfplumber / python-calamine / reportlab / mammoth / markdownify）",
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


def _pdf_inline(text: str) -> str:
    """行内 Markdown → reportlab 迷你 HTML：先转义，再解析 **加粗** 与 `代码`。"""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)


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


# ----------------------------------------------------------------- Excel


# 只有 calamine 能读的旧格式 / 非 OOXML 格式，openpyxl 读不了
_CALAMINE_ONLY_EXTS = (".xls", ".xlsb", ".ods")


def _xlsx_cell(v) -> str:
    """单元格值转字符串。calamine 把数字统一给成 float，整数值去掉多余的 .0。"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _xlsx_head(ap: str, names: list[str], target: str, nrows: int, ncols: int,
               shown_rows: int, shown_cols: int, asked: str) -> str:
    head = f"[{ap}] 工作表 {names} / 当前: {target} / {nrows} 行 × {ncols} 列"
    if asked and asked != target:
        head += f"（未找到表 {asked!r}，已回退到 {target}）"
    if nrows > shown_rows or ncols > shown_cols:
        head += f"\n…（仅显示前 {shown_rows} 行 × {shown_cols} 列）"
    return head


def _read_xlsx_calamine(ap: str, sheet: str, max_rows: int, max_cols: int) -> str:
    """python-calamine（Rust）：读取快，支持 xlsx / xlsm / xls / xlsb / ods。"""
    from python_calamine import CalamineWorkbook

    try:
        wb = CalamineWorkbook.from_path(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 解析 {os.path.splitext(ap)[1]} 失败（{type(e).__name__}: {e}）"

    names = list(wb.sheet_names)
    if not names:
        return f"[error] {ap} 没有任何工作表"
    target = sheet if sheet in names else names[0]

    ws = wb.get_sheet_by_name(target)
    nrows, ncols = int(ws.height or 0), int(ws.width or 0)
    shown_rows = min(nrows, max(1, max_rows))
    shown_cols = min(ncols, max(1, max_cols))

    data: list[list[str]] = []
    if shown_rows > 0:
        rows = ws.to_python(nrows=shown_rows)
        data = [[_xlsx_cell(c) for c in r[:shown_cols]] for r in rows]

    head = _xlsx_head(ap, names, target, nrows, ncols, shown_rows, shown_cols, sheet)
    return f"{head}\n{_md_table(data) if data else '（空表）'}"


def _read_xlsx_openpyxl(ap: str, sheet: str, max_rows: int, max_cols: int) -> str:
    """openpyxl 兜底（纯 Python，不支持 .xls/.xlsb/.ods）。"""
    try:
        import openpyxl
    except ImportError:
        return _need("openpyxl")

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

    head = _xlsx_head(ap, names, target, nrows, ncols, shown_rows, shown_cols, sheet)
    return f"{head}\n{_md_table(data) if data else '（空表）'}"


@tool(
    "read_xlsx",
    "读取 Excel 工作表并以 Markdown 表格返回，支持 .xlsx/.xlsm/.xls/.xlsb/.ods。"
    "engine=auto 时优先用 python-calamine（更快，且能读旧版 .xls），缺库自动回退 openpyxl。"
    "不给 sheet 时列出全部表名并展示第一张。",
    params={
        "path": {"type": "string", "description": "Excel 文件路径"},
        "sheet": {"type": "string", "description": "工作表名，留空取第一张", "default": ""},
        "max_rows": {"type": "integer", "description": "最多显示行数", "default": 50},
        "max_cols": {"type": "integer", "description": "最多显示列数", "default": 30},
        "engine": {"type": "string", "enum": ["auto", "calamine", "openpyxl"],
                   "description": "读取引擎", "default": "auto"},
    },
    required=["path"],
)
def read_xlsx(path: str, sheet: str = "", max_rows: int = 50, max_cols: int = 30,
              engine: str = "auto") -> str:
    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    engine = (engine or "auto").strip().lower()
    ext = os.path.splitext(ap)[1].lower()

    if engine == "openpyxl":
        if ext in _CALAMINE_ONLY_EXTS:
            return f"[error] openpyxl 不支持 {ext}，请改用 engine=calamine"
        return _read_xlsx_openpyxl(ap, sheet, max_rows, max_cols)

    try:
        import python_calamine  # noqa: F401
    except ImportError:
        if engine == "calamine" or ext in _CALAMINE_ONLY_EXTS:
            return _need("python-calamine", "python-calamine")
        return _read_xlsx_openpyxl(ap, sheet, max_rows, max_cols)

    return _read_xlsx_calamine(ap, sheet, max_rows, max_cols)


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
        return f"{head}\n（未提取到任何文字，可能是扫描件，请用 ocr_pdf 做 OCR）"
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
        return f"{head}\n（未识别到表格；可先用 read_pdf 看文字，扫描件请用 ocr_pdf）"
    return clip(head + "\n" + "\n".join(out))


# ---------------------------------------------------------------- PDF 生成


@tool(
    "write_pdf",
    "把 Markdown 文本生成 PDF：支持 # 标题、- 列表、| 表格，其余按普通段落，"
    "**加粗** 与 `代码` 会做行内样式。内置中文字体（STSong-Light），中文可直接输出。"
    "依赖第三方库 reportlab。",
    params={
        "path": {"type": "string", "description": "要写入的 pdf 路径"},
        "content": {"type": "string", "description": "Markdown 正文"},
        "title": {"type": "string", "description": "页首标题，留空则不输出标题", "default": ""},
    },
    required=["path", "content"],
)
def write_pdf(path: str, content: str, title: str = "") -> str:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        return _need("reportlab")

    ap = safe_path(path)
    os.makedirs(os.path.dirname(ap) or workspace(), exist_ok=True)

    # 中文字体：reportlab 内置的 STSong-Light（Adobe CID，无需外部字体文件）。
    # 它没有独立的粗体/斜体，用 registerFontFamily 让 <b>/<i> 回落到同一字体，避免报错。
    font = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font))
        pdfmetrics.registerFontFamily(font, normal=font, bold=font, italic=font, boldItalic=font)
    except Exception:  # noqa: BLE001
        font = "Helvetica"

    base = ParagraphStyle("base", fontName=font, fontSize=10.5, leading=16)
    bullets = ParagraphStyle("li", parent=base, leftIndent=12)
    heading = {
        n: ParagraphStyle(f"h{n}", parent=base, fontSize=s, leading=s + 6, spaceBefore=8, spaceAfter=4)
        for n, s in ((1, 18), (2, 15), (3, 13), (4, 12), (5, 11), (6, 11))
    }

    flow = []
    if title:
        flow += [Paragraph(_pdf_inline(title), heading[1]), Spacer(1, 4)]

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
                data = []
                for r in block:
                    row_cells = (r + [""] * ncols)[:ncols]
                    data.append([Paragraph(_pdf_inline(c), base) for c in row_cells])
                t = Table(data)
                t.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]))
                flow += [t, Spacer(1, 6)]
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            flow.append(Paragraph(_pdf_inline(stripped[level:].strip()), heading[min(level, 6)]))
        elif stripped.startswith(("- ", "* ")):
            flow.append(Paragraph("• " + _pdf_inline(stripped[2:].strip()), bullets))
        else:
            flow.append(Paragraph(_pdf_inline(stripped), base))
        i += 1

    try:
        SimpleDocTemplate(
            ap,
            pagesize=A4,
            leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
            title=title or os.path.basename(ap),
        ).build(flow)
    except Exception as e:  # noqa: BLE001
        return f"[error] 生成 PDF 失败（{type(e).__name__}: {e}）"

    return f"[ok] 已写入 {ap}（{os.path.getsize(ap)} 字节）"
