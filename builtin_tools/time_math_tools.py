"""
时间运算工具组。

now / convert_time 只给当前值和换算，这里补：
  time_add   某日期 +/- N 天/小时/分钟
  time_diff  两个时间差多久
"""

from __future__ import annotations

import datetime as _dt

from core import tool

TOOL_INFO = {"name": "时间运算", "version": "1.0", "author": "mini-agent"}

_FMTS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y%m%d%H%M%S",
]


def _parse(s: str) -> _dt.datetime:
    s = s.strip()
    if s.lstrip("-").isdigit():
        return _dt.datetime.fromtimestamp(int(s))
    for fmt in _FMTS:
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法识别时间：{s!r}")


@tool(
    "time_add",
    "对一个时间加上/减去指定时长，返回新时间。",
    params={
        "base": {"type": "string", "description": "基准时间（时间戳或 YYYY-MM-DD [HH:MM:SS]），留空用当前时间", "default": ""},
        "days": {"type": "integer", "description": "天数（可负）", "default": 0},
        "hours": {"type": "integer", "description": "小时数（可负）", "default": 0},
        "minutes": {"type": "integer", "description": "分钟数（可负）", "default": 0},
        "seconds": {"type": "integer", "description": "秒数（可负）", "default": 0},
    },
)
def time_add(base: str = "", days: int = 0, hours: int = 0, minutes: int = 0, seconds: int = 0) -> str:
    try:
        dt = _parse(base) if base else _dt.datetime.now()
    except ValueError as e:
        return f"[error] {e}"
    delta = _dt.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    result = dt + delta
    sign = "+" if delta.total_seconds() >= 0 else "-"
    return (
        f"基准: {dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"偏移: {sign}{abs(days)}天 {abs(hours)}时 {abs(minutes)}分 {abs(seconds)}秒\n"
        f"结果: {result.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"时间戳: {int(result.timestamp())}"
    )


@tool(
    "time_diff",
    "计算两个时间之间的差值，返回天/时/分/秒。",
    params={
        "a": {"type": "string", "description": "时间 A（时间戳或 YYYY-MM-DD [HH:MM:SS]）"},
        "b": {"type": "string", "description": "时间 B"},
    },
    required=["a", "b"],
)
def time_diff(a: str, b: str) -> str:
    try:
        da = _parse(a)
        db = _parse(b)
    except ValueError as e:
        return f"[error] {e}"
    delta = db - da
    total_sec = int(delta.total_seconds())
    sign = "B 比 A 晚" if total_sec >= 0 else "B 比 A 早"
    abs_sec = abs(total_sec)
    days, rem = divmod(abs_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return (
        f"A: {da.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"B: {db.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{sign}: {days}天 {hours}时 {minutes}分 {seconds}秒（共 {abs_sec} 秒）"
    )
