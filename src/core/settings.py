"""
通用运行时配置 —— 用户可在 Web UI 的「系统配置 → 通用配置」里自己调。

为什么独立一个模块？
  · 这些参数原先散落在环境变量与各模块默认值里，用户改一次就得重启进程；
    这里统一持久化到本机文件，改完立刻生效（同步运行时 + 重建会话 Agent）。
  · 取值优先级：显式参数 > 本文件保存的值 > 环境变量 > 内置默认值。
    这样老的 .env 用法和命令行开关都还能继续工作，不会因为上了 UI 就失效。

存储：<数据目录>/settings.json（只存用户改过的项，未改的项不落盘）
"""

from __future__ import annotations

import json
import math
import os
import re

from . import envs
from ._paths import data_dir

_SETTINGS_FILE = os.path.join(data_dir(), "settings.json")

#: 内置默认值
DEFAULTS: dict = {
    "max_turns": 10,               # 单次任务最多跑几轮（1 轮 = 一次模型调用 + 工具执行）
    "max_context_chars": 120_000,  # 上下文超过这个字符数就触发三级收敛
    "keep_recent_turns": 8,        # 滑动窗口至少保留最近几轮原文
    "max_output_chars": 8_000,     # 单个工具结果的字符上限
    "temperature": 0.0,            # 采样温度
    "stream": True,                # 流式输出（关闭则整段返回）
    "parallel_tools": True,        # 同一轮的多个工具并行执行
    "max_workers": 4,              # 并行工具的最大线程数
    # 收尾验证门：本轮执行过写文件/跑命令类操作后，模型准备直接给最终答案时，
    # 先注入一次「交付前请验证」提醒（跑测试/读回/语法检查），验证后再交付。
    # 每次任务最多触发一次，纯读类任务不触发。
    "verify_before_done": True,
    # 沙箱外读取策略：strict=沙箱外读写全拦（默认）；
    # whitelist=仅白名单路径可读；open=沙箱外全部只读放开。
    # 任何档位下沙箱外写入都由 sandbox.safe_path 硬禁，没有开关。
    "sandbox_read": "strict",
    # 沙箱外只读白名单：每行一个绝对路径（文件或目录），仅 sandbox_read=whitelist 生效
    "sandbox_read_whitelist": [],
    # ---- 记忆系统 ----
    "memory_enabled": True,        # 总开关：关闭后不注入、不自动提炼、不暴露 remember
    "memory_auto_extract": True,   # 每轮结束自动提炼偏好/决定/坑（关闭则只靠手动）
    "memory_budget_chars": 1500,   # 注入 system prompt 的记忆字符预算
    # ---- 联网搜索 ----
    # 搜索后端：deepseek 用 DeepSeek 官方 Search API（使用下面独立配置的
    # deepseek_search_api_key，不再复用大模型配置里的 Key），
    # 其余走 Tavily / 博查 / Bing 兜底。与聊天模型无关，任何厂商的模型都能用。
    "search_provider": "deepseek",
    # DeepSeek 联网专用 Key：web_search（Search API）与 fetch_url（Fetch API）共用；
    # 留空则联网搜索不可用，不会回退到大模型配置的 Key
    "deepseek_search_api_key": "",
    # ---- 运行日志 ----
    "log_enabled": True,           # 是否写本地文件日志（logs/ 下分类文件）
    "log_level": "INFO",           # DEBUG 才能看到 HTTP 访问日志
    "log_retention_days": 7,       # 按天滚动，保留最近 N 天
    # ---- 会话事件流（可追溯运行）----
    "events_enabled": True,        # 是否把运行时事实写入 <sid>.events.jsonl
    "events_value_limit": 65_536,  # 单个字符串值超过它就写成显式截断标记
    "events_keep_runs": 0,         # 预留：每会话保留最近 N 次 run 的完整事件（0=不裁剪）
    # ---- 工具暴露档位 ----
    # standard：智能体模式只常驻常用工具分组（文件/文档/代码/数据/网络/系统基础…），
    #           低频领域工具（随机值、哈希编码、压缩包、OCR、DataFrame、数据库、
    #           网络诊断…）不进请求体，省下每轮约 1.5 万字符的工具 schema；
    #           模型需要时用 search_tools 检索，命中后加载进本会话即可调用。
    # full：暴露全部工具（原来的行为），适合需要冷门工具的场景。
    "tool_profile": "standard",
    # 快速对话（chat 模式）的本地只读工具白名单开关：默认开。
    # 开启时除联网能力外，额外放行 now / time_diff / machine_status /
    # port_usage / clipboard_read / list_dir —— 都是只读、毫秒级、无副作用、
    # 不需要审批的工具，用来快速回答「现在几点 / 电脑卡不卡 / 谁占了端口」这类
    # 即时事实问题。关闭后快速对话只剩联网能力（web_search / fetch_url）。
    "chat_tools": True,
    # ---- 子智能体（委派）----
    # 开启后 delegate 工具进会话工具表：模型可把一个自包含的只读子任务交给
    # 子代理执行，只把结论带回主对话（中间过程不占主上下文）。
    "subagent_enabled": True,
    "subagent_max_turns": 10,      # 单个子任务最多几轮（实测 6 轮偏紧，简单统计也会撞上限）
    "subagent_timeout": 600,       # 单个子任务墙钟超时（秒），到点中断；给别人用时不至于被慢工具卡死
}


#: 数值型取值区间；未列出的项不做区间限制
RANGES: dict[str, tuple] = {
    "max_turns": (1, 100),
    "max_context_chars": (2_000, 2_000_000),
    "keep_recent_turns": (1, 50),
    "max_output_chars": (500, 500_000),
    "temperature": (0.0, 2.0),
    "max_workers": (1, 16),
    "memory_budget_chars": (200, 8_000),
    "log_retention_days": (1, 90),
    "events_value_limit": (4_096, 1_048_576),
    "events_keep_runs": (0, 1_000),
    "subagent_max_turns": (1, 30),
    "subagent_timeout": (10, 600),
}

#: 整数字段（其余数值字段按浮点处理）
INT_KEYS = ("max_turns", "max_context_chars", "keep_recent_turns", "max_output_chars",
            "max_workers", "memory_budget_chars", "log_retention_days",
            "events_value_limit", "events_keep_runs", "subagent_max_turns",
            "subagent_timeout")
#: 布尔字段
BOOL_KEYS = ("stream", "parallel_tools", "memory_enabled", "memory_auto_extract",
             "log_enabled", "events_enabled", "subagent_enabled", "verify_before_done",
             "chat_tools")
#: 枚举字符串字段：允许的取值
ENUM_KEYS: dict[str, tuple[str, ...]] = {
    "sandbox_read": ("strict", "whitelist", "open"),
    "log_level": ("DEBUG", "INFO", "WARNING", "ERROR"),
    "search_provider": ("deepseek", "tavily", "bocha", "bing"),
    "tool_profile": ("standard", "full"),
}
#: 字符串列表字段（元素必须是非空字符串，自动去空白）
LIST_KEYS = ("sandbox_read_whitelist",)
#: 普通字符串字段（去首尾空白、限制长度；空串合法）
STR_KEYS = ("deepseek_search_api_key",)

#: 环境变量兜底：没保存过、也没显式传参时，仍按环境变量设置来
ENV_KEYS = {
    "max_turns": "MINI_AGENT_MAX_TURNS",
    "max_context_chars": "MINI_AGENT_MAX_CONTEXT",
    "keep_recent_turns": "MINI_AGENT_KEEP_TURNS",
    "max_output_chars": "MINI_AGENT_MAX_OUTPUT",
    "stream": "MINI_AGENT_STREAM",
    "parallel_tools": "MINI_AGENT_PARALLEL",
    "max_workers": "MINI_AGENT_MAX_WORKERS",
    "tool_profile": "MINI_AGENT_TOOL_PROFILE",
}

#: 旧版名字只读兼容（模板与文档里不再出现）
LEGACY_ENV_KEYS = {
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
    raw = envs.get_str(ENV_KEYS.get(key, ""), alias=LEGACY_ENV_KEYS.get(key))
    if not raw:
        return None
    if key in BOOL_KEYS:
        low = raw.lower()
        if low in _TRUTHY:
            return True
        if low in _FALSY:
            return False
        return None
    if key in ENUM_KEYS:
        v = raw.strip()
        return v if v in ENUM_KEYS[key] else None
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

    if key in ENUM_KEYS:
        if isinstance(value, str) and value.strip() in ENUM_KEYS[key]:
            return value.strip()
        allowed = " / ".join(ENUM_KEYS[key])
        raise ValueError(f"{key} 只能是：{allowed}")

    if key in LIST_KEYS:
        # 接受数组，也接受 textarea 传来的多行字符串；元素去空白、去重、保序
        if isinstance(value, str):
            items = re.split(r"[\r\n,]+", value)
        elif isinstance(value, list):
            items = value
        else:
            raise ValueError(f"{key} 必须是字符串数组或换行分隔的文本")
        out: list[str] = []
        for it in items:
            s = str(it).strip()
            if s and s not in out:
                out.append(s)
        return out

    if key in STR_KEYS:
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串")
        return value.strip()[:200]

    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是数字")

    # NaN / inf 不能当配置值：nan 能同时绕过上下界比较（nan<lo 与 nan>hi 都是 False），
    # 而 inf 在 int() 上抛的是 OverflowError，不在上面的异常契约里 ——
    # 两者都会让「保存成功」变成「读出来是非法值 / 直接崩」。
    if not math.isfinite(num):
        raise ValueError(f"{key} 必须是有限数字")

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
    """把只影响进程全局的配置同步到运行时（工具输出上限、沙箱读取策略、日志）。"""
    from . import logging_setup, sandbox

    sandbox.set_max_output_chars(get("max_output_chars"))
    sandbox.set_read_policy(str(get("sandbox_read")), list(get("sandbox_read_whitelist") or []))
    logging_setup.reconfigure()
