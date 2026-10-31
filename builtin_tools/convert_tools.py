# 外部依赖：LibreOffice（headless）。不用 pip 安装，属于可选系统程序。
"""
文档转换工具组：把 Word / Excel / PPT 统一转成 PDF。

独立边界：
  · 只做「办公文档 → PDF」这一件事；PDF 自身的合并/拆分/水印等仍在 pdf_tools。
  · 不与任何兄弟模块互相 import；公共能力只依赖 core。
  · 依赖外部程序 LibreOffice（soffice），不引入额外 Python 包。
  · 找不到 soffice 时返回明确安装提示，不静默失败。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core import read_path, safe_path, tool, workspace

TOOL_INFO = {
    "name": "文档转换",
    "version": "1.0",
    "author": "mini-agent",
    "desc": "Word / Excel / PPT → PDF（调用 LibreOffice headless）",
}

#: 允许转换的扩展名（LibreOffice 原生支持范围里的常见办公格式）
_OFFICE_EXTS = (
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".odt", ".ods", ".odp", ".rtf",
)

#: Windows / macOS 的常见安装位置（PATH 里找不到时兜底）
_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def _find_soffice() -> str | None:
    """按 环境变量 → PATH → 常见安装路径 的顺序定位 soffice。"""
    env = (os.environ.get("MINI_AGENT_SOFFICE") or "").strip()
    if env:
        return env if os.path.isfile(env) else None

    for name in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found

    for path in _SOFFICE_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _out_dir(ap: str, out_dir: str) -> str:
    """输出目录：显式指定优先；否则跟随源文件所在目录（源文件在工作区外时落到根）。"""
    if out_dir:
        return safe_path(out_dir)
    rel = os.path.relpath(ap, workspace())
    if rel.startswith(".."):
        return safe_path(".")
    return safe_path(os.path.dirname(rel) or ".")


@tool(
    "office_to_pdf",
    "把 Word / Excel / PPT 转成 PDF（调用 LibreOffice headless）。支持 .docx/.doc/.xlsx/.xls/.pptx/.ppt/"
    ".odt/.ods/.odp/.rtf。out_dir 留空时输出到源文件同目录；输出文件已存在时直接覆盖。"
    "需要系统安装 LibreOffice，可用环境变量 MINI_AGENT_SOFFICE 指定 soffice 路径。",
    group="文档·转换",
    exclusive=True,
    params={
        "path": {"type": "string", "description": "源办公文档路径"},
        "out_dir": {"type": "string", "description": "输出目录，留空为源文件同目录", "default": ""},
        "timeout": {"type": "integer", "description": "转换超时秒数", "default": 120},
    },
    required=["path"],
)
def office_to_pdf(path: str, out_dir: str = "", timeout: int = 120) -> str:
    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    ext = os.path.splitext(ap)[1].lower()
    if ext not in _OFFICE_EXTS:
        return (
            f"[error] 不支持的格式 {ext or '(无扩展名)'}；"
            f"可用：{' / '.join(_OFFICE_EXTS)}"
        )
    if ext == ".pdf":
        return "[error] 源文件已经是 PDF，无需转换"

    soffice = _find_soffice()
    if not soffice:
        return (
            "[error] 未找到 LibreOffice（soffice）。\n"
            "  安装后重试：https://www.libreoffice.org/download/\n"
            "  或设置环境变量 MINI_AGENT_SOFFICE 指向 soffice 可执行文件。"
        )

    out_ap = _out_dir(ap, out_dir)
    os.makedirs(out_ap, exist_ok=True)
    expected = os.path.join(out_ap, os.path.splitext(os.path.basename(ap))[0] + ".pdf")

    # 独立用户配置目录：避免连到正在运行的 LibreOffice 实例而卡住。
    profile = tempfile.mkdtemp(prefix="mini-agent-lo-")
    try:
        proc = subprocess.run(
            [
                soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
                f"-env:UserInstallation={Path(profile).as_uri()}",
                "--convert-to", "pdf", "--outdir", out_ap, ap,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10, int(timeout)),
        )
    except subprocess.TimeoutExpired:
        return f"[error] LibreOffice 转换超时（>{max(10, int(timeout))}s）：{ap}"
    except Exception as e:  # noqa: BLE001
        return f"[error] 调用 LibreOffice 失败（{type(e).__name__}: {e}）"
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if not os.path.isfile(expected):
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return (
            f"[error] LibreOffice 未生成 PDF（退出码 {proc.returncode}）"
            + (f"\n{detail[:500]}" if detail else "")
        )

    return f"[ok] 已转换 → {expected}（{os.path.getsize(expected)} 字节）"
