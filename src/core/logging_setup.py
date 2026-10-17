"""
本地文件日志 —— 关键路径按类别分别落盘，便于排障。

为什么要独立一个模块？
  · 业务模块只 `logging.getLogger("mini-agent.xxx")`，文件、级别、清理策略
    全部集中在这里，改配置不用动业务代码。
  · 外部日志按天分目录：`logs/2026-10-17/app.log`（启动/HTTP/会话）、
    `llm.log`（模型调用摘要）、`tool.log`（工具执行）、`error.log`（所有 ERROR 及以上）。
  · 只落摘要，不落对话正文：用户输入与模型输出都只记字符数。

配置来源：settings.py 的 log_enabled / log_level / log_retention_days。
未初始化时挂 NullHandler，直接 import 业务模块（如单测）不会报 "No handlers"。
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import threading
import time

from ._paths import ROOT

#: 默认日志目录（项目根/logs，与 README 约定一致）
LOG_DIR = os.path.join(ROOT, "logs")

_ROOT_NAME = "mini-agent"
#: 类别 → 文件名
_CATEGORY_FILES = {
    "app": "app.log",
    "llm": "llm.log",
    "tool": "tool.log",
}
_ERROR_FILE = "error.log"

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

_FORMAT = "%(asctime)s %(levelname)-5s [%(threadName)s] %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_lock = threading.RLock()
#: 上一次生效的配置；重复 init 相同配置直接返回，避免重复 handler
_signature: tuple | None = None


# ---------------------------------------------------------------- 内部

def _level(value: str) -> int:
    return _LEVELS.get((value or "").strip().upper(), logging.INFO)


def _formatter() -> logging.Formatter:
    return logging.Formatter(_FORMAT, datefmt=_DATEFMT)


def _clear_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不影响重新配置
            pass


class _DailyDirHandler(logging.Handler):
    """按记录日期写入 `logs/<YYYY-MM-DD>/<filename>`，并清理过期日期目录。

    为什么不用 TimedRotatingFileHandler：
      它只会把文件改名成 `app.log.2026-10-17` 留在**同一个目录**，
      没法做到「每天一个目录」。这里自己管文件句柄：跨天时关旧开新。
      写入由 logging.Handler 自带的锁串行化，多线程安全。
    """

    terminator = "\n"

    def __init__(self, log_dir: str, filename: str, level: int, retention_days: int) -> None:
        super().__init__(level)
        self.log_dir = log_dir
        self.filename = filename
        self.retention_days = max(1, retention_days)
        self._stream = None
        self._day = ""
        self._last_prune = ""

    def _open(self, day: str) -> None:
        self._close_stream()
        path = os.path.join(self.log_dir, day, self.filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._stream = open(path, "a", encoding="utf-8")
        self._day = day

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    def _prune(self, day: str) -> None:
        """删除早于保留天数的日期目录（只认 YYYY-MM-DD 形状的目录）。"""
        try:
            today = datetime.datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return
        cutoff = today - datetime.timedelta(days=self.retention_days)
        try:
            for entry in os.scandir(self.log_dir):
                if not entry.is_dir():
                    continue
                try:
                    d = datetime.datetime.strptime(entry.name, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if d <= cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(record.created))
            if self._stream is None or day != self._day:
                self._open(day)
            if day != self._last_prune:
                self._prune(day)
                self._last_prune = day
            stream = self._stream
            assert stream is not None, "日志流未打开"
            stream.write(self.format(record) + self.terminator)
            stream.flush()
        except Exception:  # noqa: BLE001 —— 写日志失败不能拖垮业务
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
            super().close()


def _file_handler(directory: str, filename: str, level: int,
                  retention_days: int) -> logging.Handler:
    handler = _DailyDirHandler(directory, filename, level, retention_days)
    handler.setLevel(level)
    handler.setFormatter(_formatter())
    return handler


def _disable() -> None:
    """关掉文件日志：仅保留 NullHandler，不创建任何文件。"""
    parent = logging.getLogger(_ROOT_NAME)
    parent.setLevel(logging.CRITICAL)
    parent.propagate = False
    _clear_handlers(parent)
    parent.addHandler(logging.NullHandler())
    for name in _CATEGORY_FILES:
        child = logging.getLogger(f"{_ROOT_NAME}.{name}")
        child.setLevel(logging.CRITICAL)
        _clear_handlers(child)


def _install_null() -> None:
    """import 时的静默默认：没初始化就不写、也不往终端吐。"""
    parent = logging.getLogger(_ROOT_NAME)
    parent.setLevel(logging.CRITICAL)
    parent.propagate = False
    if not parent.handlers:
        parent.addHandler(logging.NullHandler())


_install_null()


# ---------------------------------------------------------------- 对外

def get_logger(category: str) -> logging.Logger:
    """取某类别的 logger：app / llm / tool。"""
    name = (category or "").strip().lower()
    if name not in _CATEGORY_FILES:
        name = "app"
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def init(
    log_dir: str | None = None,
    level: str = "INFO",
    retention_days: int = 7,
    enabled: bool = True,
) -> None:
    """初始化/重新初始化文件日志。幂等：相同配置重复调用不会重复挂 handler。"""
    global _signature

    directory = os.path.abspath(log_dir or LOG_DIR)
    lvl = _level(level)
    days = max(1, retention_days)
    sig = (directory, lvl, days, enabled)

    with _lock:
        if _signature == sig:
            return
        if not enabled:
            _disable()
            _signature = sig
            return
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            # 目录都建不了（权限/只读盘）→ 退回静默，别让日志拖垮主流程
            _disable()
            _signature = sig
            return

        parent = logging.getLogger(_ROOT_NAME)
        parent.setLevel(logging.DEBUG)   # 子 logger 的 ERROR 要能传播到 error.log
        parent.propagate = False
        _clear_handlers(parent)
        parent.addHandler(_file_handler(directory, _ERROR_FILE, logging.ERROR, days))

        for name, filename in _CATEGORY_FILES.items():
            child = logging.getLogger(f"{_ROOT_NAME}.{name}")
            child.setLevel(lvl)
            _clear_handlers(child)
            child.addHandler(_file_handler(directory, filename, lvl, days))
            child.propagate = True      # ERROR 继续向上，落进 error.log

        _signature = sig


def reconfigure() -> None:
    """按当前 settings 重新配置；设置页保存后调用。"""
    from . import settings

    init(
        log_dir=LOG_DIR,
        level=str(settings.get("log_level")),
        retention_days=int(settings.get("log_retention_days")),
        enabled=bool(settings.get("log_enabled")),
    )
