"""
视觉插件：让 Agent 能「看」图片。

用法（模型视角）：需要查看图片内容时调用 view_image，下一轮就能看到图。

原理：
    工具返回值永远是字符串，图片数据走 core.attach_image 的附件通道；
    主循环（agent._run_loop）在回填完本轮工具结果后取走附件，
    作为一条 user 消息追加进对话，视觉模型在下一轮真正「看到」图片。
    模型本身必须支持图片输入 —— 纯文本模型只会看到文字摘要。
"""

from __future__ import annotations

import base64
import os

from core import attach_image, safe_path, tool

# 魔数嗅探表：只认扩展名是不可靠的（demo_case 里就有刻意伪造的）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # base64 后膨胀约 1/3，太大就会吃掉大量上下文


def _sniff(data: bytes) -> str | None:
    """按魔数判断图片格式，返回 MIME 类型；认不出返回 None。"""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@tool(group="视觉")
def view_image(path: str) -> str:
    """查看工作区内的图片文件，把图片内容附加给模型分析。

    Args:
        path: 相对工作区的图片路径（支持 png / jpeg / gif / bmp / webp）
    """
    fp = safe_path(path)
    if not os.path.isfile(fp):
        return f"[error] 文件不存在：{path}"
    size = os.path.getsize(fp)
    if size == 0:
        return f"[error] 文件是空的：{path}"
    if size > MAX_IMAGE_BYTES:
        return (
            f"[error] 图片过大：{size} 字节（上限 {MAX_IMAGE_BYTES}）。"
            "请先压缩或裁剪，或用 run_python 提取关键区域。"
        )

    with open(fp, "rb") as f:
        data = f.read()

    mime = _sniff(data)
    if mime is None:
        return (
            f"[error] 不是可识别的图片格式：{path}。"
            "可能已损坏，或其实是文本/其它二进制 —— 可先用 run_shell xxd 看魔数确认。"
        )

    url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    kind = mime.split("/", 1)[1].upper()
    attach_image(url, note=f"{path}（{kind}，{size} 字节）")
    return f"[image attached] 已附加图片：{path}（{kind}，{size} 字节）"
