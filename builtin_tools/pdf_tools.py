# pip install pypdf pdfplumber reportlab pillow pypdfium2
"""
PDF 工具组（.pdf）：读取文字 / 表格、生成、合并、拆分、旋转、提取图片、图片合成、
页面转图片、水印、压缩、加密与解密。

独立边界：
  · 只处理 .pdf 与图片进 PDF；不解析 Word/Excel/PPT（转换见 convert_tools）。
  · 不与任何兄弟模块互相 import；公共能力只依赖 core。
  · 写操作统一走 safe_path，只能落在工作区内；输出文件已存在时直接覆盖。
  · 第三方库懒加载：缺库时只让当前工具返回可读提示。
"""

from __future__ import annotations

import io
import os
import re

from core import clip, read_path, safe_path, tool, workspace

TOOL_INFO = {
    "name": "PDF 文档",
    "version": "1.2",
    "author": "mini-agent",
    "desc": "读取 PDF 文字/表格、生成、合并/拆分/旋转/提取图片/图片合成、转图片、水印、压缩、加密解密",
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


def _parse_pages(spec: str, total: int, max_pages: int) -> list[int]:
    """把 "1-3,5" 解析成 0 起始的页索引；留空则取前 max_pages 页。"""
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


def _parse_ranges(spec: str, total: int, max_parts: int) -> list[list[int]]:
    """把 "1-3;5" 解析成若干页段；留空表示每页单独一段。"""
    spec = (spec or "").strip()
    if not spec:
        return [[i] for i in range(total)][: max(1, max_parts)]

    parts: list[list[int]] = []
    for seg in spec.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if "-" in seg:
            a, _, b = seg.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
        else:
            try:
                start = end = int(seg)
            except ValueError:
                continue
        idxs = [p - 1 for p in range(start, end + 1) if 1 <= p <= total]
        if idxs:
            parts.append(idxs)
    return parts[: max(1, max_parts)]


def _image_ext(name: str, data: bytes) -> str:
    """优先按原文件名取扩展名，否则按魔数嗅探。"""
    ext = os.path.splitext(name or "")[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"):
        return ext
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _safe_stem(path: str) -> str:
    """文件名主干做安全化，避免路径分隔符与空白混进输出名。"""
    stem = os.path.splitext(os.path.basename(path))[0] or "pdf"
    return re.sub(r"[^\w.-]+", "_", stem).strip("._") or "pdf"


def _pdf_inline(text: str) -> str:
    """行内 Markdown → reportlab 迷你 HTML：先转义，再解析 **加粗** 与 `代码`。"""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)


# ------------------------------------------------------------------ 读取


@tool(
    "read_pdf",
    "提取 PDF 的文字内容（按页）。扫描件/纯图片 PDF 提不出文字，需要 OCR 另行处理。"
    "依赖第三方库 pypdf。",
    group="文档·PDF",
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
    group="文档·PDF",
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


# ---------------------------------------------------------------- 生成


@tool(
    "write_pdf",
    "把 Markdown 文本生成 PDF：支持 # 标题、- 列表、| 表格，其余按普通段落，"
    "**加粗** 与 `代码` 会做行内样式。内置中文字体（STSong-Light），中文可直接输出。"
    "依赖第三方库 reportlab。",
    group="文档·PDF",
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


# ---------------------------------------------------------------- 合并 / 拆分 / 旋转


@tool(
    "pdf_merge",
    "合并多个 PDF 为一个文件（按 paths 顺序拼接全部页）。输出文件已存在时直接覆盖。"
    "依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "paths": {"type": "array", "items": {"type": "string"},
                  "description": "要合并的 PDF 路径列表，按顺序拼接"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
    },
    required=["paths", "out"],
)
def pdf_merge(paths: list, out: str) -> str:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _need("pypdf")

    if not paths:
        return "[error] paths 不能为空"
    ap_out = safe_path(out)
    os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)

    writer = PdfWriter()
    total = 0
    for p in paths:
        ap = read_path(p)
        if not os.path.isfile(ap):
            return f"[error] 文件不存在：{ap}"
        try:
            reader = PdfReader(ap)
            writer.append(ap)
            total += len(reader.pages)
        except Exception as e:  # noqa: BLE001
            return f"[error] 读取 {ap} 失败（{type(e).__name__}: {e}）"

    try:
        tmp = ap_out + ".tmp.pdf"
        writer.write(tmp)
        os.replace(tmp, ap_out)
    except Exception as e:  # noqa: BLE001
        return f"[error] 写入 PDF 失败（{type(e).__name__}: {e}）"

    return f"[ok] 已合并 {len(paths)} 个 PDF / 共 {total} 页 → {ap_out}（{os.path.getsize(ap_out)} 字节）"


@tool(
    "pdf_split",
    "按页段拆分 PDF：pages 形如 \"1-3;5\"，每个分号段输出一个文件；留空表示每页一个文件。"
    "输出目录内的同名文件会被直接覆盖。依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out_dir": {"type": "string", "description": "输出目录"},
        "pages": {"type": "string", "description": '页段，如 "1-3;5"；留空逐页拆分', "default": ""},
        "max_parts": {"type": "integer", "description": "最多输出文件数", "default": 100},
    },
    required=["path", "out_dir"],
)
def pdf_split(path: str, out_dir: str, pages: str = "", max_parts: int = 100) -> str:
    try:
        from pypdf import PdfReader, PdfWriter
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
    if total == 0:
        return f"[error] {ap} 没有页面"
    parts = _parse_ranges(pages, total, max_parts)
    if not parts:
        return "[error] pages 未匹配到任何有效页"

    out_ap = safe_path(out_dir)
    os.makedirs(out_ap, exist_ok=True)
    stem = _safe_stem(ap)
    written: list[str] = []
    for idxs in parts:
        tag = f"p{idxs[0] + 1}" if len(idxs) == 1 else f"p{idxs[0] + 1}-{idxs[-1] + 1}"
        target = safe_path(os.path.join(out_dir, f"{stem}_{tag}.pdf"))
        writer = PdfWriter()
        for i in idxs:
            writer.add_page(reader.pages[i])
        try:
            writer.write(target)
        except Exception as e:  # noqa: BLE001
            return f"[error] 写入 {target} 失败（{type(e).__name__}: {e}）"
        written.append(os.path.basename(target))

    return f"[ok] 已拆分 {len(written)} 个文件到 {out_ap}：\n" + "\n".join(f"- {n}" for n in written)


@tool(
    "pdf_rotate",
    "旋转 PDF 页面并另存：angle 只接受 90 / 180 / 270；pages 留空表示全部页。"
    "输出文件已存在时直接覆盖。依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
        "angle": {"type": "integer", "description": "旋转角度：90 / 180 / 270", "default": 90},
        "pages": {"type": "string", "description": '要旋转的页，如 "1-3"；留空表示全部', "default": ""},
    },
    required=["path", "out"],
)
def pdf_rotate(path: str, out: str, angle: int = 90, pages: str = "") -> str:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _need("pypdf")

    angle = int(angle) % 360
    if angle not in (90, 180, 270):
        return "[error] angle 只接受 90 / 180 / 270"

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    try:
        reader = PdfReader(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 解析 PDF 失败（{type(e).__name__}: {e}）"

    total = len(reader.pages)
    idxs = _parse_pages(pages, total, total)
    ap_out = safe_path(out)
    os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)

    writer = PdfWriter()
    try:
        for i in range(total):
            page = reader.pages[i]
            if i in idxs:
                page.rotate(angle)
            writer.add_page(page)
        _write_pdf(writer, ap_out, ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 旋转 PDF 失败（{type(e).__name__}: {e}）"

    return f"[ok] 已旋转 {len(idxs)}/{total} 页（{angle}°）→ {ap_out}（{os.path.getsize(ap_out)} 字节）"


@tool(
    "pdf_extract_images",
    "提取 PDF 里内嵌的位图（page.images），保存到输出目录；扫描页可能得到整页大图。"
    "不处理矢量图形。输出目录内同名文件会被直接覆盖。依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out_dir": {"type": "string", "description": "保存图片的目录"},
        "pages": {"type": "string", "description": '页号，如 "1-3"；留空表示全部', "default": ""},
        "max_images": {"type": "integer", "description": "最多提取图片数", "default": 30},
    },
    required=["path", "out_dir"],
)
def pdf_extract_images(path: str, out_dir: str, pages: str = "", max_images: int = 30) -> str:
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
    idxs = _parse_pages(pages, total, total)
    out_ap = safe_path(out_dir)
    os.makedirs(out_ap, exist_ok=True)

    limit = max(1, max_images)
    saved: list[str] = []
    for i in idxs:
        try:
            images = list(reader.pages[i].images)
        except Exception:  # noqa: BLE001 —— 个别页取不到图不影响其它页
            continue
        for img in images:
            if len(saved) >= limit:
                break
            name = getattr(img, "name", "") or "image"
            data = getattr(img, "data", b"") or b""
            if not data:
                continue
            ext = _image_ext(name, data)
            stem = _safe_stem(name)
            target = safe_path(os.path.join(out_dir, f"{stem}{ext}"))
            with open(target, "wb") as f:
                f.write(data)
            saved.append(os.path.basename(target))
        if len(saved) >= limit:
            break

    if not saved:
        return f"[{ap}] 未提取到内嵌图片（可能是纯文字或矢量图；扫描件可先用 ocr_pdf）"
    return f"[ok] 已提取 {len(saved)} 张图片到 {out_ap}：\n" + "\n".join(f"- {n}" for n in saved)


@tool(
    "images_to_pdf",
    "把多张图片按顺序合成一个 PDF，每张一页并按比例缩放居中（A4）。"
    "输出文件已存在时直接覆盖；需要第三方库 reportlab 与 pillow。",
    group="文档·PDF",
    params={
        "paths": {"type": "array", "items": {"type": "string"},
                  "description": "图片路径列表，按顺序生成"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
    },
    required=["paths", "out"],
)
def images_to_pdf(paths: list, out: str) -> str:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError:
        return _need("reportlab")
    try:
        import PIL  # noqa: F401 —— ImageReader 依赖 Pillow
    except ImportError:
        return _need("Pillow", "pillow")

    if not paths:
        return "[error] paths 不能为空"
    ap_out = safe_path(out)
    os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)

    page_w, page_h = A4
    margin = 18 * mm
    c = canvas.Canvas(ap_out, pagesize=A4)
    used = 0
    for p in paths:
        ap = read_path(p)
        if not os.path.isfile(ap):
            c.save()
            return f"[error] 文件不存在：{ap}"
        try:
            img = ImageReader(ap)
            iw, ih = img.getSize()
        except Exception as e:  # noqa: BLE001
            c.save()
            return f"[error] 读取图片 {ap} 失败（{type(e).__name__}: {e}）"
        scale = min((page_w - 2 * margin) / iw, (page_h - 2 * margin) / ih)
        w, h = iw * scale, ih * scale
        c.drawImage(img, (page_w - w) / 2, (page_h - h) / 2, width=w, height=h,
                    preserveAspectRatio=True, mask="auto")
        c.showPage()
        used += 1
    c.save()

    return f"[ok] 已合成 {used} 张图片 → {ap_out}（{os.path.getsize(ap_out)} 字节）"


# ---------------------------------------------------------------- 页面转图片


@tool(
    "pdf_to_images",
    "把 PDF 页面渲染成 PNG 图片，按页保存到输出目录。适合扫描件 OCR 前置、页面预览。"
    "输出目录内同名文件会被直接覆盖；依赖第三方库 pypdfium2。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out_dir": {"type": "string", "description": "保存 PNG 的目录"},
        "pages": {"type": "string", "description": '页号，如 "1-3"；留空取前 max_pages 页', "default": ""},
        "dpi": {"type": "integer", "description": "渲染分辨率（72-300）", "default": 150},
        "max_pages": {"type": "integer", "description": "最多渲染页数", "default": 20},
    },
    required=["path", "out_dir"],
)
def pdf_to_images(path: str, out_dir: str, pages: str = "", dpi: int = 150, max_pages: int = 20) -> str:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _need("pypdfium2")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        pdf = pdfium.PdfDocument(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 打开 PDF 失败（{type(e).__name__}: {e}）"

    total = len(pdf)
    if total == 0:
        return f"[error] {ap} 没有页面"
    idxs = _parse_pages(pages, total, max_pages)
    scale = max(72, min(int(dpi), 300)) / 72.0

    out_ap = safe_path(out_dir)
    os.makedirs(out_ap, exist_ok=True)
    stem = _safe_stem(ap)
    written: list[str] = []
    for i in idxs:
        page = pdf[i]
        try:
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            target = safe_path(os.path.join(out_dir, f"{stem}_p{i + 1}.png"))
            image.save(target, format="PNG")
            written.append(os.path.basename(target))
            try:
                bitmap.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            return f"[error] 渲染第 {i + 1} 页失败（{type(e).__name__}: {e}）"
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        pdf.close()
    except Exception:  # noqa: BLE001
        pass

    return f"[ok] 已渲染 {len(written)} 页到 {out_ap}：\n" + "\n".join(f"- {n}" for n in written)


# ---------------------------------------------------------------- 水印 / 压缩 / 加密


@tool(
    "pdf_watermark",
    "给 PDF 添加文字水印（居中旋转、半透明），中文可用。pages 留空表示全部页。"
    "输出文件已存在时直接覆盖；依赖第三方库 pypdf 与 reportlab。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
        "text": {"type": "string", "description": "水印文字"},
        "opacity": {"type": "number", "description": "不透明度（0.02-1）", "default": 0.15},
        "angle": {"type": "number", "description": "旋转角度，如 45", "default": 45},
        "pages": {"type": "string", "description": '页号，如 "1-3"；留空表示全部', "default": ""},
    },
    required=["path", "out", "text"],
)
def pdf_watermark(path: str, out: str, text: str, opacity: float = 0.15,
                  angle: float = 45, pages: str = "") -> str:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return (
            "[error] 需要第三方库 reportlab 与 pypdf，当前环境未安装。\n"
            "  安装：python -m pip install reportlab pypdf\n"
            "  （装到本项目虚拟环境，勿用系统 Python）"
        )

    text = (text or "").strip()
    if not text:
        return "[error] 水印文字不能为空"

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    try:
        reader = PdfReader(ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 解析 PDF 失败（{type(e).__name__}: {e}）"

    total = len(reader.pages)
    idxs = _parse_pages(pages, total, total)
    opacity = max(0.02, min(float(opacity), 1.0))
    angle = float(angle)

    font = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    except Exception:  # noqa: BLE001
        font = "Helvetica"

    ap_out = safe_path(out)
    os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)
    writer = PdfWriter()
    try:
        for i in range(total):
            page = reader.pages[i]
            if i in idxs:
                w = float(page.mediabox.width)
                h = float(page.mediabox.height)
                buf = io.BytesIO()
                c = canvas.Canvas(buf, pagesize=(w, h))
                c.saveState()
                c.setFillAlpha(opacity)
                c.setFont(font, max(10.0, min(w, h) * 0.06))
                c.translate(w / 2, h / 2)
                c.rotate(angle)
                c.drawCentredString(0, 0, text)
                c.restoreState()
                c.save()
                buf.seek(0)
                page.merge_page(PdfReader(buf).pages[0])
            writer.add_page(page)
        _write_pdf(writer, ap_out, ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 添加水印失败（{type(e).__name__}: {e}）"

    return f"[ok] 已给 {len(idxs)}/{total} 页添加水印 → {ap_out}（{os.path.getsize(ap_out)} 字节）"


@tool(
    "pdf_compress",
    "用 pypdf 做无损压缩：压缩内容流、合并重复对象、清理孤立对象。"
    "不保证体积明显下降（图片不会重采样）；输出文件已存在时直接覆盖。依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
    },
    required=["path", "out"],
)
def pdf_compress(path: str, out: str) -> str:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _need("pypdf")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    before = os.path.getsize(ap)
    try:
        reader = PdfReader(ap)
        writer = PdfWriter()
        for page in reader.pages:
            try:
                page.compress_content_streams()
            except Exception:  # noqa: BLE001 —— 个别页压缩失败不应中断整份文件
                pass
            writer.add_page(page)
        try:
            writer.compress_identical_objects()
        except Exception:  # noqa: BLE001
            pass
        ap_out = safe_path(out)
        os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)
        _write_pdf(writer, ap_out, ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 压缩 PDF 失败（{type(e).__name__}: {e}）"

    after = os.path.getsize(ap_out)
    delta = after - before
    pct = (delta / before * 100) if before else 0
    return (
        f"[ok] 已压缩 → {ap_out}\n"
        f"  {before} → {after} 字节（{pct:+.1f}%；pypdf 只做无损压缩，图片未重采样）"
    )


@tool(
    "pdf_encrypt",
    "给 PDF 设置打开密码（AES-128）。owner_password 留空时与打开密码相同。"
    "输出文件已存在时直接覆盖；依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
        "password": {"type": "string", "description": "打开密码（不能为空）"},
        "owner_password": {"type": "string", "description": "权限密码，留空同打开密码", "default": ""},
    },
    required=["path", "out", "password"],
)
def pdf_encrypt(path: str, out: str, password: str, owner_password: str = "") -> str:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _need("pypdf")

    if not password:
        return "[error] password 不能为空"
    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        reader = PdfReader(ap)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        try:
            writer.encrypt(password, owner_password or password, algorithm="AES-128")
        except TypeError:  # pypdf 4.x 没有 algorithm 参数
            writer.encrypt(password, owner_password or password, use_128bit=True)
        ap_out = safe_path(out)
        os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)
        _write_pdf(writer, ap_out, ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 加密 PDF 失败（{type(e).__name__}: {e}）"

    return f"[ok] 已加密 → {ap_out}（{os.path.getsize(ap_out)} 字节，AES-128）"


@tool(
    "pdf_decrypt",
    "解除 PDF 的打开密码并另存为不加密文件。源文件未加密时直接复制。"
    "输出文件已存在时直接覆盖；依赖第三方库 pypdf。",
    group="文档·PDF",
    params={
        "path": {"type": "string", "description": "源 PDF 路径"},
        "out": {"type": "string", "description": "输出 PDF 路径"},
        "password": {"type": "string", "description": "打开密码", "default": ""},
    },
    required=["path", "out"],
)
def pdf_decrypt(path: str, out: str, password: str = "") -> str:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _need("pypdf")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    try:
        reader = PdfReader(ap)
        encrypted = bool(reader.is_encrypted)
        if encrypted and not reader.decrypt(password or ""):
            return "[error] 密码错误，无法解密"
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        ap_out = safe_path(out)
        os.makedirs(os.path.dirname(ap_out) or workspace(), exist_ok=True)
        _write_pdf(writer, ap_out, ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 解密 PDF 失败（{type(e).__name__}: {e}）"

    note = "" if encrypted else "（源文件未加密，已原样复制页面）"
    return f"[ok] 已解密 → {ap_out}（{os.path.getsize(ap_out)} 字节）{note}"


def _write_pdf(writer, out_ap: str, source_ap: str) -> None:
    """输出路径与源文件相同时先写临时文件再替换，避免读一半就被截断。"""
    if os.path.abspath(out_ap) == os.path.abspath(source_ap):
        tmp = out_ap + ".tmp.pdf"
        writer.write(tmp)
        os.replace(tmp, out_ap)
    else:
        writer.write(out_ap)
