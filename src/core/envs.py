"""
系统配置项的环境变量读取 —— 统一前缀、旧名兼容、严格校验。

约定：
  · 名字统一 `MINI_AGENT_` 前缀（见项目根 .env.example）；
  · 旧版 `AGENT_*` 只作为兼容别名读取，不再出现在文档与模板里；
  · 非法值直接报错，不静默回落到默认值（否则端口写错会变成「看起来正常」）。

优先级由调用方保证：命令行参数 > 环境变量 > 内置默认值。
"""

from __future__ import annotations

import os

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def raw(name: str, alias: str | None = None) -> str:
    """取原始字符串：新名优先，然后旧名；都为空返回空串。"""
    for key in (name, alias):
        if not key:
            continue
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def get_str(name: str, default: str = "", alias: str | None = None) -> str:
    return raw(name, alias) or default


def get_int(name: str, default: int, alias: str | None = None) -> int:
    value = raw(name, alias)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} 必须是整数，当前值：{value!r}") from None


def get_bool(name: str, default: bool = False, alias: str | None = None) -> bool:
    value = raw(name, alias).lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} 必须是 0/1（或 true/false），当前值：{value!r}")
