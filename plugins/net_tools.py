"""
网络工具组。

刻意做得很小：只提供一个 GET 抓取，不提供任意抓取后执行的能力。
插件扩展的边界应该由核心决定，而不是由模型决定。
"""

from __future__ import annotations

import re
import urllib.request

from core import clip, tool
from core.cancel import Cancelled as CancelledError

PLUGIN_INFO = {"name": "网络", "version": "1.0", "author": "mini-agent"}

MAX_BYTES = 2 * 1024 * 1024


@tool(
    "fetch_url",
    "抓取一个网页/接口的文本内容（GET）。返回已剥离 HTML 标签的纯文本。",
    params={"url": {"type": "string", "description": "完整 URL（http/https）"}},
    required=["url"],
)
def fetch_url(url: str) -> str:
    if not re.match(r"^https?://", url):
        return "[error] 只支持 http/https"
    from core.cancel import current_token

    token = current_token()
    try:
        if token is not None:
            token.check()   # 发起前快速失败；urlopen 一旦阻塞只能靠它的 timeout 兜底
        req = urllib.request.Request(url, headers={"User-Agent": "mini-agent/0.2"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, "replace")
    except CancelledError:
        return "[cancelled] 抓取已被用户终止"
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"

    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return clip(text.strip())
