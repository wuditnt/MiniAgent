"""
MHTML（.mht / .mhtml）解析：把单文件网页归档拆成「主 HTML + 内嵌资源」。

为什么放后端：
  · MHTML 是 MIME multipart，浏览器 iframe 不能直接渲染；
  · Python 标准库 email 已能处理 base64 / quoted-printable，无需新依赖；
  · 资源统一转成 data: URL，前端只负责把 cid/相对路径替换掉。

不做的事：
  · 不执行脚本、不联网、不落盘；
  · 找不到 text/html 主体时抛 ValueError，由调用方转成「不支持预览」。
"""

from __future__ import annotations

import base64
import os
from email import policy
from email.parser import BytesParser
from urllib.parse import urlparse

from .sandbox import decode_bytes


def _aliases(part) -> set[str]:
    """给一个 MIME part 生成可被 HTML 引用的候选键。

    常见三种引用方式都要覆盖：
      · cid:img001@example        （Content-ID）
      · images/pic.png            （Content-Location 的相对路径）
      · https://host/path/pic.png （Content-Location 的完整 URL）
    """
    keys: set[str] = set()

    cid = (part.get("Content-ID") or "").strip().strip("<>").strip()
    if cid:
        keys.add(cid)
        keys.add(f"cid:{cid}")

    loc = (part.get("Content-Location") or "").strip()
    if loc:
        keys.add(loc)
        path = urlparse(loc).path
        if path:
            keys.add(path)
            keys.add(path.lstrip("/"))
            base = os.path.basename(path)
            if base:
                keys.add(base)

    return keys


def parse_mhtml(data: bytes, base_name: str = "") -> tuple[str, dict[str, str]]:
    """解析 MHTML，返回 (主 HTML 文本, {资源键: data URL})。

    base_name 仅用于错误信息，便于定位是哪个文件解析失败。
    """
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as e:  # noqa: BLE001 —— 交给 handler 转成 unsupported
        raise ValueError(f"MHTML 解析失败：{e}") from e

    html: str | None = None
    resources: dict[str, str] = {}

    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype.startswith("multipart/"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue

        if ctype == "text/html" and html is None:
            charset = part.get_content_charset()
            if charset:
                try:
                    html = payload.decode(charset, "replace")
                except LookupError:
                    html, _enc = decode_bytes(payload)
            else:
                html, _enc = decode_bytes(payload)
            continue

        # text/plain 往往是邮件正文的替代版本，不作为资源内联
        if ctype == "text/plain":
            continue

        data_url = (
            f"data:{ctype};base64,"
            + base64.b64encode(payload).decode("ascii")
        )
        for key in _aliases(part):
            resources.setdefault(key, data_url)

    if html is None:
        who = f"（{base_name}）" if base_name else ""
        raise ValueError(f"MHTML 归档里没有 HTML 主体{who}")
    return html, resources
