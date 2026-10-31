"""
文本处理增强工具组。

json_query   用点路径从 JSON 里取值（避免模型从长文本里硬抠字段）
url_encode/decode  URL 编解码
html_to_text  HTML 标签剥离成纯文本
case_convert  大小写/驼峰/下划线转换
"""

from __future__ import annotations

import html as _html
import json as _json
import re
import urllib.parse

from core import decode_bytes, read_bytes, read_path, tool

TOOL_INFO = {"name": "文本增强", "version": "1.0", "author": "mini-agent"}


@tool(
    "json_query",
    "从 JSON 文件或 JSON 字符串里按路径取值。路径用点号分隔，数组用 [0] 或 [*]。",
    params={
        "path": {"type": "string", "description": "JSON 文件路径；和 text 二选一", "default": ""},
        "text": {"type": "string", "description": "JSON 字符串；和 path 二选一", "default": ""},
        "query": {"type": "string", "description": "取值路径，如 data.items[0].name 或 users[*].age"},
    },
    required=["query"],
)
def json_query(path: str = "", text: str = "", query: str = "") -> str:
    if path:
        ap = read_path(path)
        raw, _ = decode_bytes(read_bytes(ap))
    else:
        raw = text
    if not raw.strip():
        return "[error] 请提供 path 或 text"
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError as e:
        return f"[error] JSON 解析失败：{e}"

    # 解析路径：拆成 tokens（支持 a.b[0].c 和 a[*]）
    tokens = re.findall(r"[^.\[\]]+|\[\*\]|\[\d+\]", query)
    results = [data]
    for tok in tokens:
        if tok == "[*]":
            expanded = []
            for r in results:
                if isinstance(r, list):
                    expanded.extend(r)
            results = expanded
        elif tok.startswith("[") and tok.endswith("]"):
            idx = int(tok[1:-1])
            results = [r[idx] for r in results if isinstance(r, (list, dict)) and idx < len(r)]
        else:
            results = [r.get(tok) for r in results if isinstance(r, dict) and tok in r]

    if not results:
        return f"[empty] 路径 {query!r} 没有匹配到值"
    if len(results) == 1:
        return _json.dumps(results[0], ensure_ascii=False, indent=2)
    return _json.dumps(results, ensure_ascii=False, indent=2)


@tool(
    "url_encode",
    "把文本 URL 编码（application/x-www-form-urlencoded）。",
    params={
        "text": {"type": "string", "description": "要编码的文本"},
        "safe": {"type": "string", "description": "不编码的字符", "default": ""},
    },
    required=["text"],
)
def url_encode(text: str, safe: str = "") -> str:
    return urllib.parse.quote(text, safe=safe)


@tool(
    "url_decode",
    "把 URL 编码的字符串解码回原文。",
    params={"text": {"type": "string", "description": "URL 编码的字符串"}},
    required=["text"],
)
def url_decode(text: str) -> str:
    return urllib.parse.unquote(text)


@tool(
    "html_to_text",
    "把 HTML 字符串剥离标签，返回纯文本。",
    params={"html": {"type": "string", "description": "HTML 内容"}},
    required=["html"],
)
def html_to_text(html: str) -> str:
    # 去掉 script/style 块
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # 块级标签换行
    text = re.sub(r"</(p|div|br|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)
    # 去标签
    text = re.sub(r"<[^>]+>", "", text)
    # 实体解码 & 清理空白
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@tool(
    "case_convert",
    "转换字符串的命名风格：驼峰 / 下划线 / 中划线 / 全大写。",
    params={
        "text": {"type": "string", "description": "要转换的字符串"},
        "to": {"type": "string", "description": "目标风格：camel / snake / kebab / upper / lower / title", "default": "snake"},
    },
    required=["text"],
)
def case_convert(text: str, to: str = "snake") -> str:
    # 先拆成单词序列
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|$)|\d+", text)
    if not words:
        words = [text]

    to = to.lower()
    if to == "camel":
        return words[0].lower() + "".join(w.capitalize() for w in words[1:])
    elif to == "pascal":
        return "".join(w.capitalize() for w in words)
    elif to == "snake":
        return "_".join(w.lower() for w in words)
    elif to == "kebab":
        return "-".join(w.lower() for w in words)
    elif to == "upper":
        return "_".join(w.upper() for w in words)
    elif to == "lower":
        return "".join(w.lower() for w in words)
    elif to == "title":
        return " ".join(w.capitalize() for w in words)
    return f"[error] 不支持的目标风格 {to!r}，可选：camel/pascal/snake/kebab/upper/lower/title"
