# pip install pypdfium2 rapidocr-onnxruntime
"""
OCR 工具组：对图片或扫描件 PDF 做文字识别。

技术栈：
  · pypdfium2        把 PDF 页面渲染成位图（自带 pdfium 二进制，无需系统安装）
  · rapidocr-onnxruntime  ONNX 版 PP-OCR，模型随包内置，同样不需要系统安装

设计取舍：
  · OCR 引擎第一次调用要初始化（几秒），用模块级缓存 + 锁，避免重复加载。
  · 两个库都懒加载：缺库只让对应工具返回提示，不影响插件整体加载。
  · 读走 read_path，与其他插件一样受路径沙箱约束；本组只读不写。
  · 扫描件识别慢且结果噪声大，页数/字符数都带上限。
"""

from __future__ import annotations

import os
import threading

from core import clip, read_path, tool

PLUGIN_INFO = {
    "name": "OCR 文字识别",
    "version": "1.0",
    "author": "mini-agent",
    "desc": "图片 / 扫描件 PDF 的文字识别（依赖 pypdfium2 / rapidocr-onnxruntime）",
}

# 单次返回的默认字符上限
DEFAULT_MAX_CHARS = 8000

_lock = threading.Lock()
_engine = None


def _need(lib: str, pip_name: str = "") -> str:
    return (
        f"[error] 需要第三方库 {lib}，当前环境未安装。\n"
        f"  安装：python -m pip install {pip_name or lib}\n"
        f"  （装到本项目虚拟环境，勿用系统 Python）"
    )


def _get_engine():
    """返回 (引擎, 错误信息)。首次调用会初始化，之后复用缓存。"""
    global _engine
    if _engine is not None:
        return _engine, ""
    with _lock:
        if _engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                return None, _need("rapidocr-onnxruntime")
            try:
                _engine = RapidOCR()
            except Exception as e:  # noqa: BLE001
                return None, f"[error] OCR 引擎初始化失败（{type(e).__name__}: {e}）"
    return _engine, ""


def _run(engine, image) -> list[str]:
    """跑一次识别，返回文本行。"""
    result, _ = engine(image)
    return [str(r[1]) for r in (result or []) if len(r) > 1]


@tool(
    "ocr_image",
    "对图片做 OCR，返回识别到的文字行。首次调用需初始化引擎（几秒）。"
    "适合截图、扫描图、照片里的文字。依赖第三方库 rapidocr-onnxruntime。",
    params={
        "path": {"type": "string", "description": "图片文件路径"},
        "max_chars": {"type": "integer", "description": "返回最大字符数", "default": DEFAULT_MAX_CHARS},
    },
    required=["path"],
)
def ocr_image(path: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    engine, err = _get_engine()
    if err:
        return err

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        lines = _run(engine, ap)
    except Exception as e:  # noqa: BLE001
        return f"[error] 识别失败（{type(e).__name__}: {e}）"

    if not lines:
        return f"[{ap}] 未识别到文字"
    return clip(f"[{ap}] 识别到 {len(lines)} 行\n" + "\n".join(lines), max_chars)


@tool(
    "ocr_pdf",
    "对扫描件 / 图片型 PDF 做 OCR：渲染前几页为图，逐页识别文字。"
    "read_pdf 提不出文字（扫描件）时用这个。首次调用需初始化引擎（几秒）。"
    "依赖第三方库 pypdfium2 与 rapidocr-onnxruntime。",
    params={
        "path": {"type": "string", "description": "pdf 文件路径"},
        "max_pages": {"type": "integer", "description": "最多识别前几页", "default": 5},
        "dpi": {"type": "integer", "description": "渲染分辨率，越高越准也越慢", "default": 200},
        "max_chars": {"type": "integer", "description": "返回最大字符数", "default": DEFAULT_MAX_CHARS},
    },
    required=["path"],
)
def ocr_pdf(path: str, max_pages: int = 5, dpi: int = 200,
            max_chars: int = DEFAULT_MAX_CHARS) -> str:
    engine, err = _get_engine()
    if err:
        return err

    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _need("pypdfium2")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    scale = max(1.0, dpi / 72)
    limit = max(1, max_pages)
    chunks: list[str] = []
    try:
        with pdfium.PdfDocument(ap) as pdf:
            total = len(pdf)
            for i in range(min(limit, total)):
                try:
                    image = pdf[i].render(scale=scale).to_numpy()
                    lines = _run(engine, image)
                except Exception as e:  # noqa: BLE001
                    lines = [f"[本页识别失败: {type(e).__name__}: {e}]"]
                chunks.append(f"--- 第 {i + 1} 页 ---\n" + ("\n".join(lines) or "(本页未识别到文字)"))
    except Exception as e:  # noqa: BLE001
        return f"[error] 渲染 PDF 失败（{type(e).__name__}: {e}）"

    head = f"[{ap}] 共 {total} 页，OCR 了前 {min(limit, total)} 页"
    return clip(f"{head}\n\n" + "\n\n".join(chunks), max_chars)
