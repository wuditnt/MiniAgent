"""
办公文档工具组测试：Word / Excel / PDF / PPT。

不需要 API Key、不花钱。所有临时文件都建在项目根下，
保证 safe_path 的沙箱判定与真实运行一致；跑完自动清理。

覆盖：
  1. 四个模块已拆分并注册，旧工具名一个不少
  2. Word / Excel 读写往返
  3. PDF 生成 / 合并 / 拆分 / 旋转 / 提取图片 / 图片合成
  4. PPTX 生成与读取
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT

from core.registry import REGISTRY  # noqa: E402

from builtin_tools import pdf_tools, pptx_tools, sheet_tools, word_tools  # noqa: E402
import tools  # noqa: E402,F401  —— 导入即加载全部内置工具


@contextmanager
def temp_dir():
    """在项目根下建临时目录：safe_path 只允许工作区内写入。"""
    path = tempfile.mkdtemp(prefix="office-test-", dir=PACKAGE_ROOT)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _skip_if_missing(*modules: str) -> bool:
    try:
        for name in modules:
            __import__(name)
    except ImportError as e:
        print(f"SKIP：缺少依赖 {e.name}")
        return True
    return False


def test_modules_and_tool_names():
    names = {t.name for t in REGISTRY.all()}
    expected = {
        "read_docx", "write_docx", "docx_to_markdown",
        "read_xlsx", "write_xlsx",
        "read_pdf", "read_pdf_tables", "write_pdf",
        "pdf_merge", "pdf_split", "pdf_rotate", "pdf_extract_images", "images_to_pdf",
        "read_pptx", "write_pptx",
    }
    missing = expected - names
    assert not missing, f"缺少工具：{missing}"

    for mod in (word_tools, sheet_tools, pdf_tools, pptx_tools):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "from builtin_tools" not in src, f"{mod.__name__} 不应依赖兄弟模块"
        assert "from ." not in src, f"{mod.__name__} 不应使用相对导入"
    print("✓ 模块拆分：15 个工具已注册，四个模块互不 import")


def test_word_roundtrip():
    if _skip_if_missing("docx"):
        return
    with temp_dir() as d:
        path = os.path.join(d, "demo.docx")
        content = "# 标题\n\n- 条目一\n- 条目二\n\n| 名称 | 数量 |\n|---|---|\n| A | 1 |"
        out = word_tools.write_docx(path, content)
        assert out.startswith("[ok]"), out

        read = word_tools.read_docx(path)
        assert "# 标题" in read, read
        assert "- 条目一" in read, read
        assert "| 名称 | 数量 |" in read, read

        if not _skip_if_missing("mammoth"):
            md = word_tools.docx_to_markdown(path)
            assert "标题" in md, md
            assert "条目一" in md, md
    print("✓ Word：写入 / 读取 / 转 Markdown 往返正确")


def test_sheet_roundtrip():
    if _skip_if_missing("openpyxl"):
        return
    with temp_dir() as d:
        path = os.path.join(d, "demo.xlsx")
        rows = [["姓名", "年龄"], ["张三", 18], ["李四", 20]]
        out = sheet_tools.write_xlsx(path, rows, "人员")
        assert out.startswith("[ok]"), out

        read = sheet_tools.read_xlsx(path, sheet="人员")
        assert "姓名" in read and "张三" in read and "18" in read, read
        assert "| 姓名 | 年龄 |" in read, read
    print("✓ Excel：写入 / 读取往返正确")


def test_pdf_tools():
    if _skip_if_missing("pypdf", "reportlab", "PIL"):
        return
    with temp_dir() as d:
        p1 = os.path.join(d, "one.pdf")
        p2 = os.path.join(d, "two.pdf")
        assert pdf_tools.write_pdf(p1, "# 第一页\nAAA", "文档一").startswith("[ok]")
        assert pdf_tools.write_pdf(p2, "# 第二页\nBBB", "文档二").startswith("[ok]")

        merged = os.path.join(d, "merged.pdf")
        out = pdf_tools.pdf_merge([p1, p2], merged)
        assert out.startswith("[ok]"), out

        text = pdf_tools.read_pdf(merged, pages="1-2", max_pages=2)
        assert "第一页" in text and "第二页" in text, text

        out_dir = os.path.join(d, "split")
        out = pdf_tools.pdf_split(merged, out_dir, pages="1;2")
        assert out.startswith("[ok]"), out
        assert len(os.listdir(out_dir)) == 2, os.listdir(out_dir)

        rotated = os.path.join(d, "rotated.pdf")
        out = pdf_tools.pdf_rotate(merged, rotated, 90, pages="1")
        assert out.startswith("[ok]"), out
        from pypdf import PdfReader
        assert int(PdfReader(rotated).pages[0].rotation or 0) == 90

        # 一张 PNG → PDF → 提取图片
        from PIL import Image
        from reportlab.pdfgen import canvas
        png = os.path.join(d, "red.png")
        Image.new("RGB", (200, 100), (220, 40, 40)).save(png)
        img_pdf = os.path.join(d, "image.pdf")
        c = canvas.Canvas(img_pdf)
        c.drawImage(png, 100, 400, width=200, height=100)
        c.showPage()
        c.save()

        out = pdf_tools.images_to_pdf([png], os.path.join(d, "from_images.pdf"))
        assert out.startswith("[ok]"), out

        out_dir = os.path.join(d, "images")
        out = pdf_tools.pdf_extract_images(img_pdf, out_dir)
        assert out.startswith("[ok]"), out
        assert os.listdir(out_dir), out
    print("✓ PDF：生成 / 合并 / 拆分 / 旋转 / 提取图片 / 图片合成全部通过")


def test_pptx_roundtrip():
    if _skip_if_missing("pptx"):
        return
    with temp_dir() as d:
        path = os.path.join(d, "demo.pptx")
        out = pptx_tools.write_pptx(
            path,
            [
                {"title": "背景", "bullets": ["现状", "目标"], "notes": "讲清楚为什么"},
                {"title": "方案", "bullets": ["步骤一", "步骤二"]},
            ],
            title="项目汇报",
        )
        assert out.startswith("[ok]"), out
        assert "3 页" in out, out

        read = pptx_tools.read_pptx(path)
        assert "项目汇报" in read, read
        assert "背景" in read and "现状" in read, read
        assert "备注：讲清楚为什么" in read, read
        assert "方案" in read and "步骤一" in read, read
    print("✓ PPTX：生成 / 读取（标题 / 要点 / 备注）正确")


if __name__ == "__main__":
    print("工作区:", PACKAGE_ROOT)
    print("---")
    test_modules_and_tool_names()
    test_word_roundtrip()
    test_sheet_roundtrip()
    test_pdf_tools()
    test_pptx_roundtrip()
    print("---")
    print("全部通过 ✅")
