"""
内联提问工具 ask_user —— 让智能体在需要澄清或抉择时，直接在对话界面里
把问题（含候选选项）摆到用户面前，点选即答，不用去输入框打字。

与 request_approval 的关系：
  · 两者共用同一套「推事件 → Event.wait 挂起工作线程 → POST 唤醒」通道；
    区别在载荷：授权是 bool（批准/拒绝），提问是文本（选中的项或自由输入）。
  · 本工具在 agent 模式的三档确认下全部暴露 —— 它不是安全闸，是交互能力，
    因此不像 request_approval 那样只在 auto 档出现。

询问通道同样是进程级 asker（由 agent 在 run 时注入 UI 桥），
这样工具函数本身保持无状态、可在任意工作线程里执行。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Callable

from .registry import REGISTRY, Tool, ToolRegistry

#: 选项数量上限（描述里引导模型给 2-6 个，这里只作硬性清点上限）
MAX_OPTIONS = 8

#: 无人工通道 / 未作答时的统一前缀，便于模型一眼识别
UNANSWERED = "[unanswered]"


def _clean_options(options: object) -> list[str]:
    """规范化选项：去空白、丢空项、保序去重、截断到上限。"""
    if not options:
        return []
    if isinstance(options, str):
        items: list = [options]
    elif isinstance(options, (list, tuple)):
        items = list(options)
    else:
        return []
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= MAX_OPTIONS:
            break
    return out


# ---------------------------------------------------------------- 询问通道

# 为什么用 ContextVar 而不是全局变量：Web UI 每个会话一个线程、可并发跑
# agent.run()。全局槽位会让 A 会话的提问弹到 B 会话，且 A 结束时把还在跑的
# B 的通道清空。ContextVar 按「运行上下文」隔离，互不干扰。
# 工具在工作线程里执行 —— agent 提交任务时会 copy_context，所以这里读得到。
_asker: ContextVar[Callable[..., str] | None] = ContextVar("ask_asker", default=None)


def set_asker(fn: Callable[..., str] | None) -> Token:
    """注入「向用户提问」的实现，返回可交给 reset_asker 的 token。

    签名约定：fn(question, options, allow_free_text, multi) -> str
    """
    return _asker.set(fn)


def reset_asker(token: Token) -> None:
    """恢复到注入前的状态；token 跨上下文无效时退化为清空本上下文。"""
    try:
        _asker.reset(token)
    except (ValueError, LookupError):
        _asker.set(None)


def has_asker() -> bool:
    return _asker.get() is not None


def ask_user(
    question: str,
    options: list | None = None,
    allow_free_text: bool = True,
    multi: bool = False,
) -> str:
    text = str(question or "").strip() or "(未提供问题)"
    opts = _clean_options(options)
    allow = bool(allow_free_text)
    many = bool(multi)

    asker = _asker.get()
    if asker is None:
        # 无 UI 桥（脚本/测试直调）：不替用户编答案，明确告知拿不到答复
        return f"{UNANSWERED} 当前环境没有人工问答通道，无法获取用户答复。"
    try:
        answer = str(asker(text, opts, allow, many) or "").strip()
    except Exception:  # noqa: BLE001 —— 询问桥故障不该中断整个任务
        return f"{UNANSWERED} 问答通道故障，未能拿到答复。请改用其它方式获取信息。"
    if not answer:
        return (
            f"{UNANSWERED} 用户未作答（跳过或任务已停止）。"
            "请基于已有信息继续，或明确说明你采用的假设。"
        )
    return f"[answer] {answer}"


def register(reg: ToolRegistry | None = None) -> None:
    """把 ask_user 注册进指定注册表（默认全局 REGISTRY）。幂等。"""
    # 不能写 `reg or REGISTRY`：ToolRegistry 有 __len__，空注册表是 falsy
    target = reg if reg is not None else REGISTRY
    if target.get("ask_user") is not None:
        return
    target.add(_ASK_USER_TOOL)


# 手动构造（不走 @tool 装饰器）：避免模块在插件加载期被首次 import 时，
# 误注册进 loader 的临时注册表。register() 由启动代码显式调用。
_ASK_USER_TOOL = Tool(
    name="ask_user",
    description=(
        "需要用户澄清或抉择才能继续时，向用户提问并等待答复。"
        "能给出候选答案就填进 options（用户可点选，也可自行输入其它答案）；"
        "没有明确候选就留空 options 走纯文本输入。"
        "默认单选：决策场景让用户从候选中拍板一个；"
        "收集信息场景（要用户并列补充多项）把 multi 设为 true 开启多选。"
        "能从上下文推断、或用默认值处理的，不要用它打断用户。"
        "不要用它替代正常回答文案。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问用户的问题，一句话，具体到可以直接判断",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选项，2-6 个；用户也可自行输入其它答案。没有明确选项时留空",
            },
            "allow_free_text": {
                "type": "boolean",
                "description": "是否允许用户自由输入，默认 true",
            },
            "multi": {
                "type": "boolean",
                "description": (
                    "是否多选，默认 false（单选）。决策/拍板场景用单选；"
                    "需要用户并列补充多项信息时设为 true"
                ),
            },
        },
        "required": ["question"],
    },
    func=ask_user,
    group="核心",
    dangerous=False,
    source="core",
    module="core.ask",
    exclusive=True,   # 提问必须串行：同一批次不能并发弹出多张卡片
)
