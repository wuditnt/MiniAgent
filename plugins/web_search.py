"""
联网搜索工具：让模型能查「今天/最新」这类时效性问题。

搜索后端可在「系统配置 → 联网搜索」里选择，与聊天模型解耦 —— 用任何厂商的
模型都能走 DeepSeek 的搜索能力：

    deepseek —— DeepSeek 官方 Search API（默认），Key 复用模型配置里的 DeepSeek
    tavily   —— 环境变量 TAVILY_API_KEY，专为 LLM 设计
    bocha    —— 环境变量 BOCHA_API_KEY，国内合规、中文质量好
    bing     —— 无需 key，抓取 cn.bing.com 结果页解析，国内可达兜底

只读、非危险；全部走 urllib，不引入新依赖。失败返回 [error] 文本，
不阻塞对话 —— 模型看到错误会自行改用已有信息作答。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

from core import clip, tool

PLUGIN_INFO = {"name": "联网搜索", "version": "1.1", "author": "mini-agent"}

MAX_BYTES = 1024 * 1024          # 单次响应最多读 1MB
SNIPPET_LEN = 200                # 每条摘要截断长度
TIMEOUT = 15
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"

#: 后端 id → 展示名（给模型看的结果抬头）
_LABELS = {"deepseek": "DeepSeek", "tavily": "Tavily", "bocha": "博查", "bing": "Bing"}


def _backend() -> str:
    """当前搜索后端：默认 deepseek，可在系统配置里切换（改完即时生效）。"""
    from core import settings

    try:
        kind = str(settings.get("search_provider") or "deepseek")
    except Exception:  # noqa: BLE001 —— 配置层异常不该让搜索工具崩掉
        kind = "deepseek"
    return kind if kind in _LABELS else "deepseek"


def _deepseek_key() -> str:
    """联网鉴权复用 DeepSeek 的 Key：优先模型配置，其次环境变量。"""
    try:
        from core import model_config

        key = model_config.provider_key("deepseek")
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    # 纯 .env 用法：接口地址指向 DeepSeek 时，主 Key 就是 DeepSeek 的 Key
    if "deepseek" in os.environ.get("OPENAI_BASE_URL", "").lower():
        return os.environ.get("OPENAI_API_KEY", "").strip()
    return ""


def _http_json(url: str, payload: dict, key: str) -> dict:
    """POST JSON 并解析响应（Tavily / 博查共用）。"""
    token = _current_token()
    if token is not None:
        token.check()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read(MAX_BYTES).decode("utf-8", "replace"))


def _current_token():
    from core.cancel import current_token

    return current_token()


def _strip_tags(markup: str) -> str:
    import html as _html

    markup = re.sub(r"<[^>]+>", " ", markup)
    # unescape 统一处理全部命名/数字实体（&nbsp; &ensp; &#0183; …）
    return re.sub(r"\s{2,}", " ", _html.unescape(markup)).strip()


# ---------------------------------------------------------------- 各后端

def _search_deepseek(query: str, n: int) -> list[tuple[str, str, str]]:
    """DeepSeek 官方 Search API（https://api-docs.deepseek.com/guides/search）。

    与聊天模型无关：任何厂商的模型都可以用这套搜索能力，鉴权复用 DeepSeek Key。
    summary 是官方为模型准备的长摘要（约 600-800 字），比 snippet 更适合喂给模型。
    """
    key = _deepseek_key()
    if not key:
        raise RuntimeError(
            "未找到 DeepSeek API Key：请先在「模型配置」里添加一条 DeepSeek 配置，"
            "或在 .env 设置 DEEPSEEK_API_KEY"
        )
    data = _http_json(
        DEEPSEEK_SEARCH_URL,
        {"query": query, "count": n, "need_content": False},
        key,
    )
    out = []
    for item in (data.get("results") or [])[:n]:
        snippet = item.get("summary") or item.get("snippet") or ""
        out.append((item.get("title", ""), item.get("url", ""), snippet))
    return out


def _search_tavily(query: str, n: int) -> list[tuple[str, str, str]]:
    data = _http_json(
        "https://api.tavily.com/search",
        {"query": query, "max_results": n, "search_depth": "basic"},
        os.environ["TAVILY_API_KEY"],
    )
    out = []
    for item in data.get("results", [])[:n]:
        out.append((item.get("title", ""), item.get("url", ""), item.get("content", "")))
    return out


def _search_bocha(query: str, n: int) -> list[tuple[str, str, str]]:
    data = _http_json(
        "https://api.bochaai.com/v1/web-search",
        {"query": query, "count": n, "summary": True},
        os.environ["BOCHA_API_KEY"],
    )
    pages = (data.get("data") or {}).get("webPages") or {}
    out = []
    for item in pages.get("value", [])[:n]:
        snippet = item.get("summary") or item.get("snippet") or ""
        out.append((item.get("name", ""), item.get("url", ""), snippet))
    return out


def _search_bing(query: str, n: int) -> list[tuple[str, str, str]]:
    """抓取 Bing 中国站搜索结果页，正则解析「标题/链接/摘要」。

    反爬风险由调用方兜底：解析为空或请求失败都算搜索失败，
    返回友好错误并提示配置 key，不阻塞对话。
    """
    token = _current_token()
    if token is not None:
        token.check()
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "count": min(n * 2, 20)}
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        html = r.read(MAX_BYTES).decode("utf-8", "replace")

    out: list[tuple[str, str, str]] = []
    # 每条结果是一个 <li class="b_algo"> 块
    for block in re.findall(r'<li class="b_algo"[\s\S]*?</li>', html)[:n]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
        if not m:
            continue
        link, title = m.group(1), _strip_tags(m.group(2))
        # 摘要在 <p class="b_lineclamp..."> 或 <div class="b_caption"><p> 里
        sm = re.search(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>([\s\S]*?)</p>', block)
        if not sm:
            sm = re.search(r'<div class="b_caption[^"]*"[^>]*>[\s\S]*?<p[^>]*>([\s\S]*?)</p>', block)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        if title and link:
            out.append((title, link, snippet))
    return out


_BACKENDS = {
    "deepseek": _search_deepseek,
    "tavily": _search_tavily,
    "bocha": _search_bocha,
    "bing": _search_bing,
}


@tool(
    "web_search",
    "联网搜索：查询实时信息、新闻、时效性问题（今天/最新/现在等）。"
    "返回按相关度排序的「标题/URL/摘要」列表。"
    "适合在回答前获取最新事实；对不变的基础知识无需调用。",
    params={
        "query": {"type": "string", "description": "搜索关键词，简洁明确，2-8 个词效果最好"},
        "max_results": {
            "type": "integer",
            "description": "返回条数，默认 5，范围 1-10",
            "default": 5,
        },
    },
    required=["query"],
)
def web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "[error] 搜索词不能为空"
    n = max(1, min(int(max_results or 5), 10))

    kind = _backend()
    label = _LABELS.get(kind, kind)
    results: list[tuple[str, str, str]] = []
    try:
        results = _BACKENDS[kind](query, n)
    except Exception as e:  # noqa: BLE001 —— 搜索失败不该阻塞对话
        return (
            f"[error] 搜索失败（后端 {label}）：{type(e).__name__}: {e}。"
            f"可在「系统配置 → 联网搜索」切换后端或检查对应的 Key。"
        )

    if not results:
        return (
            f"[error] 后端 {label} 未返回结果（可能是查询无匹配，或页面结构变化/被反爬拦截）。"
            f"可在「系统配置 → 联网搜索」切换后端。"
        )

    lines = [f"搜索「{query}」（后端：{label}）共 {len(results)} 条："]
    for i, (title, url, snippet) in enumerate(results, 1):
        snippet = snippet[:SNIPPET_LEN] + ("…" if len(snippet) > SNIPPET_LEN else "")
        lines.append(f"{i}. {title}\n   URL: {url}\n   摘要: {snippet or '（无摘要）'}")
    return clip("\n".join(lines))
