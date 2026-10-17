"""
三档确认模式与「自主判断」模式下的 request_approval 核心工具。

三档（前端输入框工具栏切换，逐轮随 /api/chat 传入，默认 ask）：
  ask   按需询问：所有 dangerous 工具执行前弹窗（历史行为）。
  auto  自主判断：普通 dangerous 工具不再弹窗，由模型按责任准则
        在「删除/批量覆盖/不可逆/影响外部/自己不确定」时主动调用
        request_approval(reason) 申请；dangerous 的 MCP 工具仍强制弹窗。
  full  完全控制：沙箱内不弹窗；dangerous 的 MCP 工具仍强制弹窗。

硬规则：MCP 工具来自外部进程，能力边界不在本地沙箱内，auto/full
两档都保留强制确认（用户已确认接受这一特例）。

request_approval 的询问通道是进程级 asker（由 agent 在 run 时注入
UI 桥），这样工具函数本身保持无状态、可在任意工作线程里执行。
"""

from __future__ import annotations

from typing import Callable

from .registry import REGISTRY, Tool, ToolRegistry

ASK = "ask"
AUTO = "auto"
FULL = "full"
MODES = (ASK, AUTO, FULL)

# auto 模式追加到 system prompt 的责任准则
AUTO_RESPONSIBILITY_PROMPT = """\
【当前确认模式：自主判断（auto）】

普通高危命令不会再逐次弹窗，改由你主动判断何时申请授权。请严格遵守：

1. 以下操作执行前，必须先调用 request_approval(reason) 说明原因，获批后再做：
   · 删除文件/目录、批量覆盖或重命名；
   · 不可逆操作（数据库写入/迁移、git 强制操作、压缩/转换覆盖原件等）；
   · 影响工作区之外的系统或外部服务（安装/卸载软件、改系统配置、发消息、提交远端等）；
   · 你不确定用户是否真的想要、或可能造成难以恢复的副作用的任何操作。
2. reason 用一句中文写清「要做什么 + 为什么需要 + 影响范围」，让用户能直接判断。
3. 用户拒绝后不要换个写法重试同一操作；应停下来说明备选方案。
4. 只读侦察、工作区内的常规读写与可随时撤销的小改动，不必申请，直接做。"""


def normalize(mode: str | None) -> str:
    """把外部传入值收敛到合法档位；非法/为空一律回落到 ask。"""
    return mode if mode in MODES else ASK


# ---------------------------------------------------------------- 询问通道

_asker: Callable[[str], bool] | None = None


def set_asker(fn: Callable[[str], bool] | None) -> None:
    """注入「向用户申请授权」的实现（agent.run 时设置，结束后置空）。"""
    global _asker
    _asker = fn


def register(reg: ToolRegistry | None = None) -> None:
    """把 request_approval 注册进指定注册表（默认全局 REGISTRY）。幂等。"""
    target = reg or REGISTRY
    if target.get("request_approval") is not None:
        return
    target.add(_REQUEST_APPROVAL_TOOL)


def request_approval(reason: str) -> str:
    text = str(reason or "").strip() or "(未填写理由)"
    asker = _asker
    if asker is None:
        # 无 UI 桥（脚本/测试直调）：不替用户做拒绝决定，默认放行并留痕
        return "[approved] 当前环境没有人工确认通道，默认放行。"
    try:
        ok = bool(asker(text))
    except Exception:  # noqa: BLE001 —— 询问桥故障时按拒绝处理更安全
        return "[denied] 授权询问失败，为安全起见按未获批准处理。请换方案或稍后再试。"
    if ok:
        return "[approved] 用户已批准该操作，可以继续。"
    return (
        "[denied] 用户没有批准该操作。请停止执行原计划，"
        "如确有必要，向用户说明备选方案或更充分的理由。"
    )


# 手动构造（不走 @tool 装饰器）：避免模块在插件加载期被首次 import 时，
# 误注册进 loader 的临时注册表。register() 由启动代码显式调用。
_REQUEST_APPROVAL_TOOL = Tool(
    name="request_approval",
    description=(
        "在自主判断模式下，就高风险/不可逆/影响外部的操作向用户申请授权。"
        "仅在操作确实有风险时调用；只读侦察与可撤销的小改动不要调用。"
        "返回 [approved] 才能继续；返回 [denied] 必须停止该操作。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "一句中文说明：要做什么、为什么需要、影响范围",
            },
        },
        "required": ["reason"],
    },
    func=request_approval,
    group="核心",
    dangerous=False,
    source="core",
    module="core.approval",
    exclusive=True,   # 授权询问必须串行：同一批次不能并发弹多个窗
)
