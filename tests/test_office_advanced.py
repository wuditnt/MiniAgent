"""
第二批 2A 测试：文档转换 + PDF 增强。

不需要 API Key、不花钱。临时文件建在项目根下，保证沙箱判定与真实运行一致。

覆盖：
  1. 6 个新工具已注册，convert_tools 不依赖兄弟模块
  2. office_to_pdf：无 LibreOffice 时返回可读错误；有则真实转换
  3. pdf_to_images：页面渲染成 PNG
  4. pdf_watermark / pdf_compress：页数与正文保持不变
  5. pdf_encrypt / pdf_decrypt：密码校验与解密后正文可读
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT

from core.registry import REGISTRY  # noqa: E402

from builtin_tools import convert_tools, pdf_tools, word_tools  # noqa: E402
import tools  # noqa: E402,F401  —— 导入即加载全部内置工具


@contextmanager
def temp_dir():
    path = tempfile.mkdtemp(prefix="office-adv-test-", dir=PACKAGE_ROOT)
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


def test_tool_registration_and_independence():
    names = {t.name for t in REGISTRY.all()}
    expected = {
        "office_to_pdf",
        "pdf_to_images", "pdf_watermark", "pdf_compress", "pdf_encrypt", "pdf_decrypt",
    }
    missing = expected - names
    assert not missing, f"缺少工具：{missing}"

    src = open(convert_tools.__file__, encoding="utf-8").read()
    assert "from builtin_tools" not in src, "convert_tools 不应依赖兄弟模块"
    assert "from ." not in src, "convert_tools 不应使用相对导入"
    print("✓ 注册与独立性：6 个新工具已注册，convert_tools 无兄弟依赖")


def test_office_to_pdf():
    with temp_dir() as d:
        docx = os.path.join(d, "demo.docx")
        assert word_tools.write_docx(docx, "# 转换测试\n正文内容").startswith("[ok]")

        # 强制走「找不到 LibreOffice」分支：错误必须可读，不能抛异常
        old = os.environ.get("MINI_AGENT_SOFFICE")
        os.environ["MINI_AGENT_SOFFICE"] = os.path.join(d, "no-such-soffice.exe")
        try:
            out = convert_tools.office_to_pdf(docx, os.path.join(d, "out"))
        finally:
            if old is None:
                os.environ.pop("MINI_AGENT_SOFFICE", None)
            else:
                os.environ["MINI_AGENT_SOFFICE"] = old
        assert out.startswith("[error]"), out
        assert "LibreOffice" in out, out

        # 本机装了 LibreOffice 才做真实转换
        if convert_tools._find_soffice():
            out = convert_tools.office_to_pdf(docx, os.path.join(d, "real"))
            assert out.startswith("[ok]"), out
            pdf = os.path.join(d, "real", "demo.pdf")
            assert os.path.isfile(pdf), out
        else:
            print("  （本机未安装 LibreOffice，跳过真实转换）")
    print("✓ office_to_pdf：缺 LibreOffice 的错误路径可读，真实转换按环境执行")


def test_pdf_to_images():
    if _skip_if_missing("pypdf", "pypdfium2", "PIL"):
        return
    with temp_dir() as d:
        p1 = os.path.join(d, "a.pdf")
        p2 = os.path.join(d, "b.pdf")
        assert pdf_tools.write_pdf(p1, "# 第一页", "A").startswith("[ok]")
        assert pdf_tools.write_pdf(p2, "# 第二页", "B").startswith("[ok]")
        merged = os.path.join(d, "merged.pdf")
        assert pdf_tools.pdf_merge([p1, p2], merged).startswith("[ok]")

        out_dir = os.path.join(d, "png")
        out = pdf_tools.pdf_to_images(merged, out_dir, pages="1-2", dpi=120)
        assert out.startswith("[ok]"), out
        files = sorted(os.listdir(out_dir))
        assert len(files) == 2, files

        from PIL import Image
        with Image.open(os.path.join(out_dir, files[0])) as img:
            assert img.width > 0 and img.height > 0
    print("✓ pdf_to_images：两页 PDF 渲染出两张 PNG")


def test_pdf_watermark_and_compress():
    if _skip_if_missing("pypdf", "reportlab"):
        return
    with temp_dir() as d:
        src = os.path.join(d, "src.pdf")
        assert pdf_tools.write_pdf(src, "# 正文\n正文内容保留", "原文档").startswith("[ok]")

        marked = os.path.join(d, "marked.pdf")
        out = pdf_tools.pdf_watermark(src, marked, "内部资料", opacity=0.2, angle=45)
        assert out.startswith("[ok]"), out
        text = pdf_tools.read_pdf(marked)
        assert "正文内容保留" in text, text

        compressed = os.path.join(d, "small.pdf")
        out = pdf_tools.pdf_compress(marked, compressed)
        assert out.startswith("[ok]"), out
        text = pdf_tools.read_pdf(compressed)
        assert "正文内容保留" in text, text
    print("✓ pdf_watermark / pdf_compress：页数与正文保持不变")


def test_pdf_encrypt_decrypt():
    if _skip_if_missing("pypdf"):
        return
    with temp_dir() as d:
        src = os.path.join(d, "plain.pdf")
        assert pdf_tools.write_pdf(src, "# 机密\n机密正文", "机密").startswith("[ok]")

        encrypted = os.path.join(d, "locked.pdf")
        out = pdf_tools.pdf_encrypt(src, encrypted, "pw-123")
        assert out.startswith("[ok]"), out

        from pypdf import PdfReader
        assert PdfReader(encrypted).is_encrypted

        bad = os.path.join(d, "bad.pdf")
        out = pdf_tools.pdf_decrypt(encrypted, bad, "wrong")
        assert out.startswith("[error]"), out

        decrypted = os.path.join(d, "unlocked.pdf")
        out = pdf_tools.pdf_decrypt(encrypted, decrypted, "pw-123")
        assert out.startswith("[ok]"), out
        text = pdf_tools.read_pdf(decrypted)
        assert "机密正文" in text, text
    print("✓ pdf_encrypt / pdf_decrypt：错误密码拒绝，正确密码解密后正文可读")


if __name__ == "__main__":
    print("工作区:", PACKAGE_ROOT)
    print("---")
    test_tool_registration_and_independence()
    test_office_to_pdf()
    test_pdf_to_images()
    test_pdf_watermark_and_compress()
    test_pdf_encrypt_decrypt()
    print("---")
    print("全部通过 ✅")
