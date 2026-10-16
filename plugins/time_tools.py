"""
时间工具组。

模型对「现在几点」「这个时间戳是哪年」这类问题经常答不准，
与其让它猜，不如直接给工具。
"""

from __future__ import annotations

import datetime as _dt
import time as _time

from core import tool

PLUGIN_INFO = {"name": "时间", "version": "1.0", "author": "mini-agent"}


@tool(
    "now",
    "返回当前时间（本地时间与 UTC 时间，带时区偏移）。",
)
def now() -> str:
    local = _dt.datetime.now()
    utc = _dt.datetime.utcnow()
    offset = local - utc
    sign = "+" if offset.total_seconds() >= 0 else "-"
    h = abs(int(offset.total_seconds() // 3600))
    return (
        f"本地时间 : {local.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"UTC      : {utc.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"时区偏移 : UTC{sign}{h:02d}:00\n"
        f"Unix 时间: {int(_time.time())}"
    )


@tool(
    "convert_time",
    "在「Unix 时间戳」与「可读时间字符串」之间互转。"
    "输入一个时间戳（数字）返回可读时间；输入时间字符串返回时间戳。",
    params={
        "value": {"type": "string", "description": "时间戳（如 1730000000）或时间字符串（如 2024-10-27 10:30:00）"},
    },
    required=["value"],
)
def convert_time(value: str) -> str:
    v = value.strip()
    # 纯数字 → 时间戳 → 可读
    if v.lstrip("-").isdigit():
        ts = int(v)
        local = _dt.datetime.fromtimestamp(ts)
        utc = _dt.datetime.utcfromtimestamp(ts)
        return (
            f"时间戳 {ts}\n"
            f"  本地: {local.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  UTC : {utc.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    # 时间字符串 → 时间戳。支持几种常见格式
    fmts = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y%m%d%H%M%S",
    ]
    for fmt in fmts:
        try:
            dt = _dt.datetime.strptime(v, fmt)
            ts = int(dt.timestamp())
            return f"{v} → Unix 时间戳 {ts}（本地时间解析）"
        except ValueError:
            continue
    return f"[error] 无法识别为时间戳或时间字符串：{v!r}\n支持格式：纯数字、YYYY-MM-DD [HH:MM[:SS]]、YYYY/MM/DD"
