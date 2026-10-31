"""
网络工具组。

`fetch_url` 有两套实现，按本轮联网模式（core.net_mode）分派：

  · cloud —— DeepSeek Fetch API（POST /v1/tools/fetch），返回干净 Markdown，
             与 Chat 接口同一把 Key，按成功请求计费
  · local —— 本地 urllib 直连，剥掉 HTML 标签；免费，但拿到的正文较粗

云端失败（403/404/429/5xx、超时、未配置 Key）时自动回退本地，保证工具可用；
用户主动取消则直接终止，不做回退。

刻意做得很小：只提供一个 GET 抓取，不提供任意抓取后执行的能力。
工具扩展的边界应该由核心决定，而不是由模型决定。
"""

from __future__ import annotations

import json
import re
import urllib.request

from core import clip, net_mode, tool
from core.cancel import Cancelled as CancelledError

TOOL_INFO = {"name": "网络", "version": "1.2", "author": "mini-agent"}

MAX_BYTES = 2 * 1024 * 1024
LOCAL_TIMEOUT = 20
CLOUD_TIMEOUT = 30
DEEPSEEK_FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"


def _check_cancel() -> None:
    from core.cancel import current_token

    token = current_token()
    if token is not None:
        token.check()


def _deepseek_key() -> str:
    """DeepSeek Fetch 也用「系统配置 → 联网搜索」里独立配置的 Key。"""
    try:
        from core import settings

        return str(settings.get("deepseek_search_api_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _cloud_fetch(url: str) -> str:
    """DeepSeek Fetch：返回 Markdown 正文；任何失败都抛异常，由调用方回退本地。"""
    _check_cancel()
    key = _deepseek_key()
    if not key:
        raise RuntimeError("未配置 DeepSeek 联网 Key（系统配置 → 联网搜索）")
    req = urllib.request.Request(
        DEEPSEEK_FETCH_URL,
        data=json.dumps({"url": url}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as r:
        data = json.loads(r.read(MAX_BYTES).decode("utf-8", "replace"))
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if not content:
        raise RuntimeError("DeepSeek Fetch 未返回正文")
    head = f"标题：{title}\nURL：{url}\n\n" if title else f"URL：{url}\n\n"
    return clip(head + content)


def _local_fetch(url: str) -> str:
    """本地直连抓取：剥脚本/样式/标签，压成纯文本。"""
    _check_cancel()
    req = urllib.request.Request(url, headers={"User-Agent": "mini-agent/0.2"})
    with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
        raw = r.read(MAX_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, "replace")
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return clip(text.strip())


@tool(
    "fetch_url",
    "抓取一个网页/接口的文本内容（GET）。"
    "联网开启时优先走 DeepSeek Fetch（返回干净的 Markdown 正文）；"
    "未开启或云端失败时回退本地抓取（剥离 HTML 标签）。",
    params={"url": {"type": "string", "description": "完整 URL（http/https）"}},
    required=["url"],
)
def fetch_url(url: str) -> str:
    if not re.match(r"^https?://", url):
        return "[error] 只支持 http/https"

    if net_mode.is_cloud():
        try:
            return _cloud_fetch(url)
        except CancelledError:
            return "[cancelled] 抓取已被用户终止"
        except Exception:  # noqa: BLE001 —— 云端不可用就回退本地
            pass

    try:
        return _local_fetch(url)
    except CancelledError:
        return "[cancelled] 抓取已被用户终止"
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"
