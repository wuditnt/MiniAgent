"""
文件日志测试。不需要 API Key，不花任何费用。

覆盖六处：
  1. 分类落盘 —— app / llm / tool 按日期目录分文件，互不串台
  2. 错误聚合 —— ERROR 同时进类别文件与 error.log
  3. 幂等重配 —— 相同配置重复 init 不重复挂 handler
  4. 关闭日志 —— enabled=False 时不创建任何目录/文件
  5. 配置校验 —— 级别枚举、保留天数区间、布尔开关
  6. 过期清理 —— 超过保留天数的日期目录被删除，非日期目录不动
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

from core import logging_setup, settings  # noqa: E402

_ROOT = logging.getLogger("mini-agent")
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _tmp() -> str:
    return tempfile.mkdtemp(prefix="ma-log-")


def _read(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _day_dir(d: str) -> str:
    """取唯一的日期子目录（logs/<YYYY-MM-DD>）。"""
    subs = [n for n in os.listdir(d) if _DATE_DIR.match(n)]
    assert len(subs) == 1, subs
    return os.path.join(d, subs[0])


# ============================================================ 1. 分类落盘

def test_category_routing():
    d = _tmp()
    try:
        logging_setup.init(log_dir=d, level="DEBUG", retention_days=3, enabled=True)
        logging_setup.get_logger("app").info("hello-app")
        logging_setup.get_logger("llm").info("hello-llm")
        logging_setup.get_logger("tool").info("hello-tool")

        day = _day_dir(d)
        assert datetime.date.today().isoformat() == os.path.basename(day)
        app = _read(os.path.join(day, "app.log"))
        llm = _read(os.path.join(day, "llm.log"))
        tool = _read(os.path.join(day, "tool.log"))
        assert "hello-app" in app and "hello-llm" not in app, app
        assert "hello-llm" in llm and "hello-tool" not in llm, llm
        assert "hello-tool" in tool and "hello-app" not in tool, tool
        # 未产生 ERROR 时不写 error.log
        assert not os.path.exists(os.path.join(day, "error.log"))
        print("✓ 分类落盘：logs/<日期>/ 下 app / llm / tool 各归各文件")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 2. 错误聚合

def test_error_aggregation():
    d = _tmp()
    try:
        logging_setup.init(log_dir=d, level="INFO", retention_days=3, enabled=True)
        logging_setup.get_logger("tool").error("boom-tool")
        logging_setup.get_logger("llm").info("normal-info")

        day = _day_dir(d)
        tool = _read(os.path.join(day, "tool.log"))
        err = _read(os.path.join(day, "error.log"))
        assert "boom-tool" in tool, tool
        assert "boom-tool" in err, err
        assert "normal-info" not in err, err
        print("✓ 错误聚合：ERROR 同时进类别文件与 error.log，INFO 不混入")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 3. 幂等重配

def test_reconfigure_idempotent():
    d = _tmp()
    try:
        logging_setup.init(log_dir=d, level="INFO", retention_days=7, enabled=True)
        first = len(logging_setup.get_logger("app").handlers)
        logging_setup.init(log_dir=d, level="INFO", retention_days=7, enabled=True)
        second = len(logging_setup.get_logger("app").handlers)
        assert first == second == 1, (first, second)

        # 换级别 → 仍只有一个 handler，且消息按新级别过滤
        logging_setup.init(log_dir=d, level="WARNING", retention_days=7, enabled=True)
        assert len(logging_setup.get_logger("app").handlers) == 1
        logging_setup.get_logger("app").info("should-not-appear")
        logging_setup.get_logger("app").warning("should-appear")
        app = _read(os.path.join(_day_dir(d), "app.log"))
        assert "should-not-appear" not in app and "should-appear" in app, app
        print("✓ 幂等重配：handler 不重复，级别变更立即生效")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 4. 关闭日志

def test_disabled_writes_nothing():
    d = _tmp()
    try:
        logging_setup.init(log_dir=d, level="DEBUG", retention_days=7, enabled=False)
        logging_setup.get_logger("app").error("silent")
        logging_setup.get_logger("tool").error("silent")
        assert os.listdir(d) == [], os.listdir(d)
        assert len(_ROOT.handlers) == 1 and isinstance(_ROOT.handlers[0], logging.NullHandler)
        print("✓ 关闭日志：不创建目录/文件，仅保留 NullHandler")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 5. 配置校验

def test_settings_coerce():
    assert settings.coerce("log_enabled", "false") is False
    assert settings.coerce("log_enabled", True) is True
    assert settings.coerce("log_level", "DEBUG") == "DEBUG"
    assert settings.coerce("log_retention_days", 30) == 30
    for bad in ("debug", "TRACE", ""):
        try:
            settings.coerce("log_level", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法级别未被拒绝：{bad!r}")
    for bad in (0, 91, 1.5):
        try:
            settings.coerce("log_retention_days", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法保留天数未被拒绝：{bad!r}")
    print("✓ 配置校验：级别枚举 / 天数 1–90 整数 / 布尔开关")


# ============================================================ 6. 过期清理

def test_prune_old_days():
    d = _tmp()
    try:
        today = datetime.date.today()
        old = (today - datetime.timedelta(days=10)).isoformat()
        recent = (today - datetime.timedelta(days=2)).isoformat()
        other = "misc"
        for name in (old, recent, other):
            os.makedirs(os.path.join(d, name))

        logging_setup.init(log_dir=d, level="INFO", retention_days=7, enabled=True)
        logging_setup.get_logger("app").info("trigger-prune")

        assert not os.path.exists(os.path.join(d, old)), "过期目录未清理"
        assert os.path.isdir(os.path.join(d, recent)), "保留期内目录被误删"
        assert os.path.isdir(os.path.join(d, other)), "非日期目录被误删"
        print("✓ 过期清理：删除超过保留天数的日期目录，非日期目录不动")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    print("项目根:", HERE)
    print("---")
    test_category_routing()
    test_error_aggregation()
    test_reconfigure_idempotent()
    test_disabled_writes_nothing()
    test_settings_coerce()
    test_prune_old_days()
    print("---")
    print("全部通过 ✅")
