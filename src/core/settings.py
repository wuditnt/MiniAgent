"""
通用运行时配置 —— 用户可在 Web UI 的「系统配置 → 通用配置」里自己调。

为什么独立一个模块？
  · 这些参数原先散落在环境变量与各模块默认值里，用户改一次就得重启进程；
    这里统一持久化到本机文件，改完立刻生效（同步运行时 + 重建会话 Agent）。
  · 取值优先级：显式参数 > 本文件保存的值 > 环境变量 > 内置默认值。
    这样老的 .env 用法和命令行开关都还能继续工作，不会因为上了 UI 就失效。

存储：web_data/settings.json（只存用户改过的项，未改的项不落盘）
"""

from __future__ import annotations

import json
import os

from ._paths import ROOT

_SETTINGS_FILE = os.path.join(ROOT, "web_data", "settings.json")

#: 内置默认值
DEFAULTS: dict = {
    "max_turns": 25,               # 单次任务最多跑几轮（1 轮 = 一次模型调用 + 工具执行）
    "max_context_chars": 120_000,  # 上下文超过这个字符数就触发三级收敛
    "keep_recent_turns": 8,        # 滑动窗口至少保留最近几轮原文
    "max_output_chars": 8_000,     # 单个工具结果的字符上限
    "temperature": 0.0,            # 采样温度
    "stream": True,                # 流式输出（关闭则整段返回）
    "parallel_tools": True,        # 同一轮的多个工具并行执行
    "max_workers": 4,              # 并行工具的最大线程数
}

#: 数值型取值区间；未列出的项不做区间限制
RANGES: dict[str, tuple] = {
    "max_turns": (1, 100),
    "max_context_chars": (2_000, 2_000_000),
    "keep_recent_turns": (1, 50),
    "max_output_chars": (500, 500_000),
    "temperature": (0.0, 2.0),
    "max_workers": (1, 16),
}

#: 整数字段（其余数值字段按浮点处理）
INT_KEYS = ("max_turns", "max_context_chars", "keep_recent_turns", "max_output_chars", "max_workers")
#: 布尔字段
BOOL_KEYS = ("stream", "parallel_tools")

#: 环境变量兜底：没保存过、也没显式传参时，仍按老 .env 设置来
ENV_KEYS = {
    "max_turns": "AGENT_MAX_TURNS",
    "max_context_chars": "AGENT_MAX_CONTEXT",
    "keep_recent_turns": "AGENT_KEEP_TURNS",
    "max_output_chars": "AGENT_MAX_OUTPUT",
    "stream": "AGENT_STREAM",
    "parallel_tools": "AGENT_PARALLEL",
    "max_workers": "AGENT_MAX_WORKERS",
}

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def keys() -> tuple[str, ...]:
    return tuple(DEFAULTS)


# ---------------------------------------------------------------- 读写

def _load_saved() -> dict:
    if not os.path.exists(_SETTINGS_FILE):
        return {}
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in DEFAULTS}


def _save_saved(data: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SETTINGS_FILE)


def _env_value(key: str):
    """环境变量兜底值；没设或写错都返回 None（当作「没设置」）。"""
    raw = os.environ.get(ENV_KEYS.get(key, ""), "").strip()
    if not raw:
        return None
    if key in BOOL_KEYS:
        low = raw.lower()
        if low in _TRUTHY:
            return True
        if low in _FALSY:
            return False
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return int(v) if key in INT_KEYS else v


def coerce(key: str, value):
    """校验并规范化单个配置值；不合法时抛 ValueError（供 API 直接回给前端）。"""
    if key not in DEFAULTS:
        raise ValueError(f"未知配置项：{key}")

    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUTHY + _FALSY:
            return value.strip().lower() in _TRUTHY
        raise ValueError(f"{key} 必须是布尔值")

    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是数字")

    if key in INT_KEYS:
        if num != int(num):
            raise ValueError(f"{key} 必须是整数")
        num = int(num)

    lo, hi = RANGES.get(key, (None, None))
    if lo is not None and num < lo:
        raise ValueError(f"{key} 不能小于 {lo}")
    if hi is not None and num > hi:
        raise ValueError(f"{key} 不能大于 {hi}")
    return num


# ---------------------------------------------------------------- 对外

def get(key: str):
    """单个配置项的有效值：保存值 > 环境变量 > 内置默认。"""
    saved = _load_saved()
    if key in saved:
        try:
            return coerce(key, saved[key])
        except ValueError:
            pass    # 文件被手改坏了 → 退回下一优先级
    from_env = _env_value(key)
    if from_env is not None:
        return from_env
    return DEFAULTS[key]


def get_all() -> dict:
    return {k: get(k) for k in DEFAULTS}


def defaults() -> dict:
    return dict(DEFAULTS)


def update(patch: dict) -> dict:
    """按 patch 更新（只覆盖出现的键），校验通过才落盘。返回更新后的全量值。"""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("没有需要更新的配置项")
    saved = _load_saved()
    for k, v in patch.items():
        saved[k] = coerce(k, v)
    _save_saved(saved)
    return get_all()


def reset() -> dict:
    """清空用户改动，回到「环境变量 / 内置默认」。"""
    if os.path.exists(_SETTINGS_FILE):
        try:
            os.remove(_SETTINGS_FILE)
        except OSError:
            pass
    return get_all()


def apply_to_runtime() -> None:
    """把只影响进程全局的配置同步到运行时（目前只有工具输出上限）。"""
    from . import sandbox

    sandbox.set_max_output_chars(get("max_output_chars"))
