"""
哈希 / 校验 / 编码工具组。

file_hash 给文件做指纹，text_hash 给字符串做指纹，base64 负责编解码。
都用 stdlib，无外部依赖。
"""

from __future__ import annotations

import base64 as _b64
import hashlib
import os

from core import read_bytes, safe_path, tool

PLUGIN_INFO = {"name": "哈希与编码", "version": "1.0", "author": "mini-agent"}

_SUPPORTED = ("md5", "sha1", "sha256", "sha512")


@tool(
    "file_hash",
    "计算文件的哈希值，用于校验文件完整性或做指纹。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "algo": {"type": "string", "description": "算法：md5 / sha1 / sha256 / sha512", "default": "sha256"},
    },
    required=["path"],
)
def file_hash(path: str, algo: str = "sha256") -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    algo = algo.lower()
    if algo not in _SUPPORTED:
        return f"[error] 不支持的算法 {algo!r}，可选：{', '.join(_SUPPORTED)}"
    h = hashlib.new(algo)
    # 分块读，大文件也不会爆内存
    with open(ap, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return f"[{ap}]\n  算法: {algo}\n  哈希: {h.hexdigest()}"


@tool(
    "text_hash",
    "计算文本字符串的哈希值。",
    params={
        "text": {"type": "string", "description": "要计算的文本"},
        "algo": {"type": "string", "description": "算法：md5 / sha1 / sha256 / sha512", "default": "sha256"},
    },
    required=["text"],
)
def text_hash(text: str, algo: str = "sha256") -> str:
    algo = algo.lower()
    if algo not in _SUPPORTED:
        return f"[error] 不支持的算法 {algo!r}，可选：{', '.join(_SUPPORTED)}"
    return hashlib.new(algo, text.encode("utf-8")).hexdigest()


@tool(
    "base64_encode",
    "把文本或二进制内容编码成 Base64 字符串。",
    params={
        "text": {"type": "string", "description": "要编码的文本（留空则从 path 读文件）", "default": ""},
        "path": {"type": "string", "description": "可选：从文件读取内容进行编码", "default": ""},
    },
)
def base64_encode(text: str = "", path: str = "") -> str:
    if path:
        ap = safe_path(path)
        if not os.path.isfile(ap):
            return f"[error] 文件不存在：{ap}"
        data = read_bytes(ap)
    else:
        data = text.encode("utf-8")
    encoded = _b64.b64encode(data).decode("ascii")
    # 每 76 字符换行，方便阅读
    wrapped = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
    return f"[base64] 共 {len(data)} 字节 → {len(encoded)} 字符\n{wrapped}"


@tool(
    "base64_decode",
    "把 Base64 字符串解码回文本。",
    params={"data": {"type": "string", "description": "Base64 字符串"}},
    required=["data"],
)
def base64_decode(data: str) -> str:
    try:
        raw = _b64.b64decode(data, validate=False)
    except Exception as e:  # noqa: BLE001
        return f"[error] Base64 解码失败：{e}"
    # 尝试按 UTF-8 当文本返回；失败就给十六进制预览
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        from core import hexdump

        return f"[binary] 解码后是二进制数据（{len(raw)} 字节），十六进制预览：\n{hexdump(raw)}"
