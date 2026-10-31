"""
图片附件通道 —— 工具把图片「递」给模型的唯一正规途径。

为什么需要它？
    工具的返回值永远是字符串（「工具与模型通过文本对话」是本项目的一条约定，
    见 user_tools/README.md），但图片必须以 content 数组的 image_url 分片
    进消息才能被视觉模型看到。所以工具的返回值只写一句「附了什么」，
    真正的图片数据走这条旁路：

        工具调用 attach_image(data_url)
            → 主循环回填完本轮工具结果后 take_images()
            → 作为一条 user 消息追加进对话
            → 视觉模型在下一轮真正「看到」图片

为什么是模块级而不是挂在 Agent 上？
    与取消令牌（core/cancel.py）同样的理由：工具运行在工作线程里，
    线程局部反而读不到。模块级收集 + 主线程批量取走，最简单也最正确。
    取走即清空 —— 每轮的附件只属于那一轮，绝不串场。
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_PENDING: list[dict] = []   # [{url, note}, ...]

# 附件说明的固定开头。它既是给模型看的提示，也是「这条 user 消息不是用户
# 说的」的识别标志 —— 历史会话文件里没留下 _injected 私有标记，只能靠它兜底。
ATTACH_NOTE = "（本轮工具附加了以下图片，请结合上文分析）"


def attach_image(url: str, note: str = "") -> None:
    """登记一张待发送图片（data URL）。任何工具都可以调用，不抛异常。"""
    with _LOCK:
        _PENDING.append({"url": url, "note": note})


def take_images() -> list[dict]:
    """取走全部待发送图片（取走即清空），组装成 user 消息的 content 分片。

    返回空列表表示本轮没有附件。分片以一条文字开头（列出每张图的说明），
    兼容个别不接受「纯图片消息」的服务端，也让模型知道图从哪来。
    """
    with _LOCK:
        pending = _PENDING.copy()
        _PENDING.clear()
    if not pending:
        return []

    notes = [p["note"] for p in pending if p.get("note")]
    parts: list[dict] = []
    if notes:
        parts.append({"type": "text", "text": ATTACH_NOTE + "\n" + "\n".join(notes)})
    parts.extend({"type": "image_url", "image_url": {"url": p["url"]}} for p in pending)
    return parts
