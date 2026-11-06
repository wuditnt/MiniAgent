"""准备 Electron 打包资源：只读应用目录 + 可移植 Python 运行时。

产物（均已 gitignore，构建时重建）：
    build/desktop-resources/app/     项目源码 + 工具/技能 + web/dist（安装目录只读）
    build/desktop-resources/python/  可移植 Python（解释器 + 依赖），工具仍由真实解释器执行

用法：
    python scripts/build_desktop.py             # 全量准备（web/dist 不存在时会先构建前端）
    python scripts/build_desktop.py --skip-web  # 复用已有 web/dist

之后：
    cd desktop && npm install && npm run dist
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "desktop-resources"
APP_OUT = OUT / "app"
PY_OUT = OUT / "python"

#: 应用目录内容：后端源码、工具目录、技能、MCP 配置、内置文档、前端产物
APP_ITEMS = (
    "src",
    "builtin_tools",
    "user_tools",
    "skills",
    "skills_user",
    "mcp",
    "docs",
    "web/dist",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
)

#: 复制 Python 安装时要跳过的体积大户（不影响运行）
PY_SKIP_DIRS = {"Doc", "Tools", "Scripts", "__pycache__", "site-packages"}
PY_SKIP_LIB = {"test", "idlelib", "turtledemo", "lib2to3"}
#: 32 位 polars 运行时：64 位解释器用不到，却占 160MB+
PY_SKIP_EXTRA = {"_polars_runtime_32"}


def _ignore_pycache(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=_ignore_pycache, symlinks=False)


def build_web() -> None:
    web = ROOT / "web"
    dist = web / "dist" / "index.html"
    if dist.exists():
        return
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("错误：web/dist 不存在，且找不到 npm；请先 cd web && npm run build")
    print("构建前端 …")
    subprocess.run([npm, "run", "build"], cwd=web, check=True)


def copy_app() -> None:
    _rmtree(APP_OUT)
    APP_OUT.mkdir(parents=True)
    for item in APP_ITEMS:
        src = ROOT / item
        if not src.exists():
            print(f"  · 跳过（不存在）：{item}")
            continue
        dst = APP_OUT / item
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            _copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        print(f"  · {item}")


def _copy_python_tree(src: Path, dst: Path) -> None:
    """复制 Python 安装树；跳过测试/文档等与运行无关的目录。"""
    for base, dirs, files in os.walk(src):
        rel = Path(base).relative_to(src)
        # Lib/site-packages 单独处理（用项目环境里的那一份）
        if rel.parts[:2] == ("Lib", "site-packages"):
            dirs[:] = []
            continue
        if rel.parts[:1] == ("Lib",) and rel.parts[1:2] and rel.parts[1] in PY_SKIP_LIB:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in PY_SKIP_DIRS and d not in PY_SKIP_EXTRA]
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(Path(base) / name, dst / rel / name)


def copy_python() -> None:
    base = Path(sys.base_prefix)
    if os.name != "nt":
        raise SystemExit("错误：桌面打包目前只支持 Windows")
    # 依赖必须取「项目实际在用的那一份」：用系统 python 跑本脚本时
    # sysconfig 给的是基础解释器的 site-packages，里面没有 openai 等项目依赖。
    venv_site = ROOT / ".venv" / "Lib" / "site-packages"
    site = venv_site if venv_site.is_dir() else Path(sysconfig.get_paths()["purelib"])
    if not (base / "python.exe").exists():
        raise SystemExit(f"错误：{base} 下找不到 python.exe")

    _rmtree(PY_OUT)
    PY_OUT.mkdir(parents=True)
    print(f"复制 Python 运行时：{base} → {PY_OUT}")
    _copy_python_tree(base, PY_OUT)

    target_site = PY_OUT / "Lib" / "site-packages"
    target_site.mkdir(parents=True, exist_ok=True)
    print(f"复制项目依赖：{site} → {target_site}")
    _copy_python_tree(site, target_site)


def verify_python() -> None:
    py = PY_OUT / "python.exe"
    # 逐个真实 import：OCR / 文档依赖牵一发动全身（比如删掉 polars 的 32 位运行时后必须确认 64 位仍可用）
    code = (
        "import importlib\n"
        "mods = ['openai','docx','openpyxl','PIL','pypdf','tkinter','pdfplumber','polars','cv2','onnxruntime']\n"
        "ok = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "        ok.append(m)\n"
        "    except Exception as e:\n"
        "        print('IMPORT_FAIL', m, repr(e))\n"
        "        raise SystemExit(1)\n"
        "import sys\n"
        "print('python', sys.version.split()[0], '| ok:', ','.join(ok))\n"
    )
    # -S 跳过 site 初始化会丢依赖；这里用普通模式，验证的正是打包后真实启动方式
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True, encoding="utf-8")
    print((r.stdout or "").strip() or (r.stderr or "").strip())
    if r.returncode != 0:
        raise SystemExit("错误：打包后的 Python 无法导入依赖")


def main() -> int:
    ap = argparse.ArgumentParser(description="准备 Electron 打包资源")
    ap.add_argument("--skip-web", action="store_true", help="不检查/构建前端（复用 web/dist）")
    args = ap.parse_args()

    os.chdir(ROOT)
    if not args.skip_web:
        build_web()
    if not (ROOT / "web" / "dist" / "index.html").exists():
        raise SystemExit("错误：web/dist/index.html 不存在，请先构建前端")

    print("复制应用文件 …")
    copy_app()
    copy_python()
    verify_python()
    print("\n完成。下一步：cd desktop && npm install && npm run dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
