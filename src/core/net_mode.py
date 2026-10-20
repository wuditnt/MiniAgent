"""
本轮联网模式 —— 决定网络工具走「云端 DeepSeek 接口」还是「本地直连」。

为什么要独立一个模块？
  · Web 层每个会话一个 worker 线程，工具执行就在该线程内；
    用 threading.local 隔离，避免并发会话互相串模式。
  · 默认 local：命令行 / 未显式开启联网时，抓取走本地，不产生云端费用。

取值：
  cloud —— web_search 用配置里的云端搜索后端，fetch_url 用 DeepSeek Fetch
  local —— 不提供搜索；fetch_url 本地直连抓取（云端失败也会回退到这里）
"""

from __future__ import annotations

import threading

_MODES = ("cloud", "local")
_DEFAULT = "local"

_local = threading.local()


def set_mode(mode: str) -> None:
    """设置当前线程的联网模式（非 cloud 一律视为 local）。"""
    _local.mode = "cloud" if str(mode).strip().lower() == "cloud" else "local"


def get_mode() -> str:
    return getattr(_local, "mode", _DEFAULT)


def is_cloud() -> bool:
    return get_mode() == "cloud"


def reset() -> None:
    """恢复默认（worker 结束时调用，避免线程复用残留）。"""
    _local.mode = _DEFAULT
