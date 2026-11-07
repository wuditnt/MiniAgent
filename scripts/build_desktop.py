"""准备 Electron 打包资源：只读应用目录 + 可移植 Python 运行时。

产物（均已 gitignore，构建时重建）：
    build/desktop-resources/app/           项目源码 + 工具/技能 + web/dist（安装目录只读）
    build/desktop-resources/python/        可移植 Python（解释器 + 基础依赖）
    build/desktop-resources/ocr-pack.zip   OCR 组件包（首次使用 OCR 时才解压到数据目录）

为什么 OCR 单独打包：
    NSIS 安装耗时主要花在逐个小文件写盘上。OCR 依赖（cv2 / onnxruntime / sympy /
    rapidocr / numpy 等）约 230MB、近 3000 个文件，只有调用 ocr_image / ocr_pdf
    时才用得到，所以不进 site-packages，改成一个 zip；安装阶段只写这一个文件。
    运行时由 src/core/extra_libs.py 负责解压与注入 sys.path。

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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "desktop-resources"
APP_OUT = OUT / "app"
PY_OUT = OUT / "python"
PACK_OUT = OUT / "ocr-pack.zip"

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

#: site-packages 里与运行无关的开发工具（只影响打包，开发机 .venv 不动）：
#: pipreqs → IPython → jedi/parso/pygments/tornado/prompt_toolkit 这一条链
#: 占 7 千个文件，却没有任何代码 import 它。
SITE_SKIP = (
    "pipreqs", "ipython", "jedi", "parso", "pygments", "tornado", "prompt_toolkit",
    "wcwidth", "traitlets", "asttokens", "executing", "stack_data",
    "matplotlib_inline", "decorator", "pure_eval", "yarg", "docopt",
    "pytest", "_pytest", "pluggy", "iniconfig",
)
#: 老版本 pytest 的兼容模块（单独成文件）
SITE_SKIP_FILES = {"py.py"}

#: OCR 依赖的顶层条目前缀：移入 ocr-pack，不铺进 site-packages。
#: 前缀同时匹配包目录与 dist-info（如 numpy-2.x.dist-info）。
OCR_PREFIXES = (
    "cv2", "opencv", "onnxruntime", "sympy", "mpmath", "numpy", "rapidocr",
    "pyclipper", "shapely", "yaml", "coloredlogs", "humanfriendly",
    "flatbuffers", "protobuf", "google", "tqdm",
)

#: 基础依赖自检：启动与常用工具必须能导入（不含 OCR 组件包里的库）
BASE_MODS = [
    "openai", "docx", "openpyxl", "PIL", "pypdf", "tkinter",
    "pdfplumber", "polars", "reportlab", "pypdfium2", "pptx",
]
#: OCR 组件包自检：解压后（PYTHONPATH 指向暂存目录）必须能导入
OCR_MODS = [
    "cv2", "numpy", "shapely", "pyclipper", "yaml", "google.protobuf",
    "onnxruntime", "rapidocr_onnxruntime",
]


def _ignore_pycache(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=_ignore_pycache, symlinks=False)


def _copy_entry(src: Path, dst: Path) -> None:
    if src.is_dir():
        _copytree(src, dst)
    else:
        shutil.copy2(src, dst)


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


def _is_dev_only(name: str) -> bool:
    low = name.lower()
    if low.startswith("__"):
        return True
    if low in SITE_SKIP_FILES:
        return True
    return any(low.startswith(prefix) for prefix in SITE_SKIP)


def _is_ocr(name: str) -> bool:
    low = name.lower()
    return any(low.startswith(prefix) for prefix in OCR_PREFIXES)


def copy_python() -> Path:
    """复制解释器 + 基础依赖，OCR 依赖摊到 ocr-pack 暂存目录。返回暂存目录。"""
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
    ocr_stage = OUT / "ocr-pack"
    _rmtree(ocr_stage)
    ocr_stage.mkdir(parents=True)
    print(f"复制项目依赖：{site} → {target_site}")
    skipped: list[str] = []
    packed: list[str] = []
    for entry in sorted(site.iterdir()):
        if entry.name in PY_SKIP_EXTRA or entry.name in PY_SKIP_DIRS:
            skipped.append(entry.name)
            continue
        if _is_dev_only(entry.name):
            skipped.append(entry.name)
            continue
        if _is_ocr(entry.name):
            _copy_entry(entry, ocr_stage / entry.name)
            packed.append(entry.name)
            continue
        _copy_entry(entry, target_site / entry.name)
    print(f"  · 剔除无关依赖 {len(skipped)} 项（开发工具 / 32 位运行时）")
    print(f"  · 移入 OCR 组件包 {len(packed)} 项")
    return ocr_stage


def _check_imports(py: Path, mods: list[str], extra_env: dict | None = None) -> None:
    """用指定解释器逐个 import，失败直接中断构建。"""
    code = (
        "import importlib\n"
        f"mods = {mods!r}\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as e:\n"
        "        print('IMPORT_FAIL', m, repr(e))\n"
        "        raise SystemExit(1)\n"
        "print('ok:', ','.join(mods))\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # 避免开发机的环境变量污染校验结果
    if extra_env:
        env.update(extra_env)
    # -S 跳过 site 初始化会丢依赖；这里用普通模式，验证的正是打包后真实启动方式
    r = subprocess.run(
        [str(py), "-c", code], capture_output=True, text=True, encoding="utf-8", env=env
    )
    print((r.stdout or "").strip() or (r.stderr or "").strip())
    if r.returncode != 0:
        raise SystemExit(f"错误：打包后的 Python 依赖校验失败（{mods}）")


def verify_python(py: Path) -> None:
    """基础依赖必须齐全；同时确认 OCR 库确实已经被移出基础依赖。"""
    _check_imports(py, BASE_MODS)
    code = (
        "import importlib.util as u\n"
        "left = [m for m in ('cv2', 'onnxruntime', 'numpy', 'sympy') if u.find_spec(m)]\n"
        "if left:\n"
        "    print('STILL_PACKED', ','.join(left))\n"
        "    raise SystemExit(1)\n"
        "print('ok: OCR 组件已移出基础依赖')\n"
    )
    r = subprocess.run(
        [str(py), "-c", code], capture_output=True, text=True, encoding="utf-8"
    )
    print((r.stdout or "").strip() or (r.stderr or "").strip())
    if r.returncode != 0:
        raise SystemExit("错误：OCR 依赖仍在基础 site-packages 中（OCR_PREFIXES 未覆盖）")


def verify_ocr_pack(py: Path, stage: Path) -> None:
    """用暂存目录冒充已解压状态，验证组件包自身依赖完整。"""
    _check_imports(py, OCR_MODS, {"PYTHONPATH": str(stage)})


def build_ocr_pack(stage: Path) -> Path:
    if PACK_OUT.exists():
        PACK_OUT.unlink()
    with zipfile.ZipFile(PACK_OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage).as_posix())
    with zipfile.ZipFile(PACK_OUT) as z:
        bad = z.testzip()
    if bad:
        raise SystemExit(f"错误：OCR 组件包损坏（{bad}）")
    return PACK_OUT


def _stat(path: Path) -> tuple[float, int]:
    mb = files = 0
    for p in path.rglob("*"):
        if p.is_file():
            mb += p.stat().st_size
            files += 1
    return mb / 1048576, files


def print_summary(pack: Path) -> None:
    print("\n打包资源：")
    for label, path in (("app", APP_OUT), ("python", PY_OUT)):
        mb, files = _stat(path)
        print(f"  · {label:8} {mb:7.1f} MB {files:6d} files")
    print(f"  · {'ocr-pack':8} {pack.stat().st_size / 1048576:7.1f} MB（已压缩，安装时只写 1 个文件）")


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
    ocr_stage = copy_python()
    py = PY_OUT / "python.exe"
    verify_python(py)
    verify_ocr_pack(py, ocr_stage)
    pack = build_ocr_pack(ocr_stage)
    _rmtree(ocr_stage)
    print_summary(pack)
    print("\n完成。下一步：cd desktop && npm install && npm run dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
