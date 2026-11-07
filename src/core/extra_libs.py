"""
桌面端的按需组件包：把 OCR 依赖从安装主流程挪到首次使用。

为什么：
    NSIS 安装耗时主要花在逐个小文件写盘上。OCR 这套依赖（cv2 / onnxruntime /
    sympy / rapidocr / numpy …）约 230MB、近 3000 个文件，但只有调用
    ocr_image / ocr_pdf 时才用得到。桌面版改为随附一个 ocr-pack.zip，
    首次 OCR 时解压到数据目录并注入 sys.path。

约定：
    MINI_AGENT_EXTRA_PACK —— 组件包路径（Electron 注入；不配则按解释器位置推断
                            <resources>/python/python.exe → <resources>/ocr-pack.zip）
    MINI_AGENT_EXTRA_LIBS —— 解压目录（默认 <数据目录>/pylibs/ocr）

行为：
    · 包不存在（源码运行 / 浏览器版）：什么都不做，依赖本来就在解释器里；
    · 已解压且包没变（按大小 + mtime 判定）：直接注入，零开销；
    · 首次或包已更新：解压到临时目录后整体替换，再注入；
    · 注入时同步写 PYTHONPATH，让 run_python / MCP 等子进程也能 import。

失败不抛异常：OCR 工具要把原因展示给用户，而不是让整个任务崩掉。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import zipfile

from core._paths import data_dir

#: 组件包路径 / 解压目录的环境变量（见模块文档）
PACK_ENV = "MINI_AGENT_EXTRA_PACK"
LIBS_ENV = "MINI_AGENT_EXTRA_LIBS"

_lock = threading.Lock()
#: 只有注入成功才置 True；后续调用直接短路，避免每次都 stat + 读标记
_injected = False


def _logger():
    try:
        from core import logging_setup

        return logging_setup.get_logger("app")
    except Exception:  # noqa: BLE001 —— 日志初始化失败不影响组件准备
        return None


def pack_path() -> str:
    """组件包路径；找不到返回空串（表示当前环境不需要/不支持按需组件）。"""
    raw = os.environ.get(PACK_ENV, "").strip().strip('"')
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    # 安装布局：resources\python\python.exe → resources\ocr-pack.zip
    guess = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))), "ocr-pack.zip"
    )
    return guess if os.path.isfile(guess) else ""


def libs_dir() -> str:
    """解压目标目录（数据目录下，安装目录只读）。"""
    raw = os.environ.get(LIBS_ENV, "").strip().strip('"')
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return os.path.join(data_dir(), "pylibs", "ocr")


def _stamp(pack: str) -> str:
    st = os.stat(pack)
    return f"{st.st_size}:{int(st.st_mtime)}"


def _inject(target: str) -> None:
    """把目录插到 sys.path 最前，并同步到 PYTHONPATH 供子进程继承。"""
    global _injected
    if target not in sys.path:
        sys.path.insert(0, target)
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if os.path.normcase(target) not in {os.path.normcase(p) for p in parts}:
        os.environ["PYTHONPATH"] = os.pathsep.join([target, *parts])
    _injected = True


def _extract(pack: str, target: str) -> None:
    """解压到临时目录后整体替换，避免解压中断留下半成品。"""
    tmp = f"{target}.tmp{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(pack) as z:
        z.extractall(tmp)
    with open(os.path.join(tmp, ".ready"), "w", encoding="utf-8") as f:
        f.write(_stamp(pack))
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)


def ensure() -> str:
    """确保额外组件可用。返回空串表示 OK，否则返回给用户看的错误文本。"""
    if _injected:
        return ""
    pack = pack_path()
    if not pack:
        return ""  # 源码运行 / 浏览器版：依赖在解释器里，无需处理
    if not os.path.isfile(pack):
        return (
            f"[error] 找不到 OCR 组件包：{pack}\n"
            f"  安装包可能不完整，建议重新安装应用。"
        )

    target = libs_dir()
    marker = os.path.join(target, ".ready")
    with _lock:
        if _injected:
            return ""
        try:
            stale = True
            try:
                with open(marker, encoding="utf-8") as f:
                    stale = f.read().strip() != _stamp(pack)
            except OSError:
                stale = True
            if stale:
                log = _logger()
                if log:
                    log.info("extralibs.extract pack=%s target=%s", pack, target)
                _extract(pack, target)
            _inject(target)
            return ""
        except Exception as e:  # noqa: BLE001 —— 原因要展示给用户
            log = _logger()
            if log:
                log.exception("extralibs.failed pack=%s target=%s", pack, target)
            return (
                f"[error] OCR 组件准备失败（{type(e).__name__}: {e}）\n"
                f"  组件包：{pack}\n"
                f"  解压目录：{target}\n"
                f"  可删除该目录后重试；反复失败建议重新安装应用。"
            )
