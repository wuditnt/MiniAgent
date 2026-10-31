"""
图片元信息工具组。

image_info    查看图片尺寸/格式/色彩模式/EXIF
image_convert 格式转换 / 缩放 / 裁剪

优先用 Pillow；没装时给出安装提示，不崩。
"""

from __future__ import annotations

import os

from core import read_path, safe_path, tool

TOOL_INFO = {"name": "图片元信息", "version": "1.0", "author": "mini-agent"}


def _has_pillow():
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


@tool(
    "image_info",
    "查看图片的元信息：格式、尺寸、色彩模式、文件大小、EXIF。",
    params={"path": {"type": "string", "description": "图片文件路径"}},
    required=["path"],
)
def image_info(path: str) -> str:
    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    size = os.path.getsize(ap)
    if not _has_pillow():
        return (
            f"[{ap}]\n  大小: {size:,} 字节\n"
            f"  [提示] 安装 Pillow 后可查看更多元信息：pip install pillow"
        )
    from PIL import Image, ExifTags

    try:
        with Image.open(ap) as im:
            fmt = im.format
            w, h = im.size
            mode = im.mode
            exif_lines = []
            try:
                exif = im._getexif() or {}
                for tag_id, value in exif.items():
                    name = ExifTags.TAGS.get(tag_id, tag_id)
                    exif_lines.append(f"      {name}: {value}")
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        return f"[error] 无法读取图片：{e}"

    lines = [
        f"[{ap}]",
        f"  格式 : {fmt}",
        f"  尺寸 : {w} × {h}",
        f"  模式 : {mode}",
        f"  大小 : {size:,} 字节",
    ]
    if exif_lines:
        lines.append("  EXIF:")
        lines.extend(exif_lines[:30])
    return "\n".join(lines)


@tool(
    "image_convert",
    "转换图片格式 / 缩放 / 裁剪。",
    params={
        "path": {"type": "string", "description": "源图片路径"},
        "output": {"type": "string", "description": "输出路径（扩展名决定格式，如 .png / .jpg）"},
        "scale": {"type": "number", "description": "缩放比例（0.5=缩小一半），与 width/height 互斥", "default": 1.0},
        "width": {"type": "integer", "description": "目标宽度（像素），留空按比例", "default": 0},
        "height": {"type": "integer", "description": "目标高度（像素），留空按比例", "default": 0},
        "quality": {"type": "integer", "description": "JPEG 质量 1-95", "default": 90},
    },
    required=["path", "output"],
    dangerous=True,
)
def image_convert(path: str, output: str, scale: float = 1.0, width: int = 0, height: int = 0, quality: int = 90) -> str:
    if not _has_pillow():
        return "[error] 需要 Pillow：pip install pillow"
    from PIL import Image

    ap = read_path(path)   # 源图只读：可来自沙箱外（受读取策略限制）
    op = safe_path(output) # 输出严格：只能写工作区内
    if not os.path.isfile(ap):
        return f"[error] 源文件不存在：{ap}"
    try:
        with Image.open(ap) as im:
            w, h = im.size
            if width or height:
                new_w = width or int(w * (height / h))
                new_h = height or int(h * (width / w))
                im = im.resize((new_w, new_h), Image.LANCZOS)
            elif scale != 1.0:
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            # RGBA 转 JPEG 需要拼白底
            fmt = os.path.splitext(op)[1].lower().lstrip(".")
            save_kwargs = {}
            if fmt in ("jpg", "jpeg"):
                if im.mode in ("RGBA", "P"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1] if im.mode == "RGBA" else None)
                    im = bg
                save_kwargs["quality"] = quality
            im.save(op, **save_kwargs)
            nw, nh = im.size
        return f"[ok] {ap} → {op}（{nw}×{nh}）"
    except Exception as e:  # noqa: BLE001
        return f"[error] 转换失败：{e}"
