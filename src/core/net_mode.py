"""
本轮联网模式 —— 决定网络工具走「云端 DeepSeek 接口」还是「本地直连」。

为什么要独立一个模块？
  · Web 层每个会话一个 worker 线程，工具执行在该线程（或它的并行子线程）内；
    用 ContextVar 隔离，避免并发会话互相串模式。
  · 原本用 threading.local 也能做到「线程间隔离」，但 ThreadPoolExecutor 起的新线程
    读不到父线程的值 —— 并行批次里的网工具会静默退回 local。agent 提交工具任务时
    已用 contextvars.copy_context() 传播上下文，所以这里必须换成 ContextVar 才生效。
  · 默认 local：命令行 / 未显式开启联网时，抓取走本地，不产生云端费用。

取值：
  cloud —— web_search 用配置里的云端搜索后端，fetch_url 用 DeepSeek Fetch
  local —— 不提供搜索；fetch_url 本地直连抓取（云端失败也会回退到这里）
"""

from __future__ import annotations

import contextvars

_MODES = ("cloud", "local")
_DEFAULT = "local"

_MODE: contextvars.ContextVar[str] = contextvars.ContextVar("net_mode", default=_DEFAULT)


def set_mode(mode: str) -> None:
    """设置当前上下文的联网模式（非 cloud 一律视为 local）。"""
    _MODE.set("cloud" if str(mode).strip().lower() == "cloud" else "local")


def get_mode() -> str:
    return _MODE.get()


def is_cloud() -> bool:
    return get_mode() == "cloud"


def reset() -> None:
    """恢复默认（worker 结束时调用，避免上下文复用残留）。"""
    _MODE.set(_DEFAULT)
