"""
示例用户插件：文本统计。

这个文件的重点是演示「零样板」——
函数上只有类型注解和一个 docstring，schema 会被自动推导出来：

    def count_lines(path: str, top: int = 5) -> str

    path  是必填参数，自动映射成 {"type": "string"} 且进入 required
    top   有默认值 5，自动映射成 {"type": "integer", "default": 5} 且不进 required
    描述   自动从 docstring 的 Args 段里逐个抽取

也就是说：你不需要手写任何 JSON，写个普通函数就完事了。
"""

from __future__ import annotations

import re
from collections import Counter

from core import read_path, tool

PLUGIN_INFO = {
    "name": "文本统计",
    "version": "1.0",
    "author": "示例",
    "desc": "演示自动 Schema 推导的最小插件",
}


@tool
def count_lines(path: str, top: int = 5) -> str:
    """统计文本文件的总行数、字符数，并列出最长的前若干行。

    Args:
        path: 相对工作区的文件路径
        top: 列出最长的前 N 行，默认 5

    Returns:
        统计结果文本
    """
    try:
        text = open(read_path(path), encoding="utf-8", errors="replace").read()
    except FileNotFoundError:
        return f"[error] 文件不存在：{path}"

    lines = text.splitlines()
    ranked = sorted(enumerate(lines, 1), key=lambda kv: len(kv[1]), reverse=True)[:max(1, top)]
    detail = "\n".join(f"    第{no}行（{len(s)} 字符）: {s[:80]}" for no, s in ranked)
    return (
        f"[{path}] 共 {len(lines)} 行 / {len(text)} 字符\n"
        f"  最长的 {len(ranked)} 行：\n{detail}"
    )


@tool(
    "word_freq",
    "统计文本中词频最高的若干词。按空白与常见标点切分，忽略单字符。",
    params={"top": {"type": "integer", "description": "返回前 N 个高频词", "default": 10}},
    required=["path"],
)
def word_freq(path: str, top: int = 10) -> str:
    try:
        text = open(read_path(path), encoding="utf-8", errors="replace").read()
    except FileNotFoundError:
        return f"[error] 文件不存在：{path}"

    words = [w.lower() for w in re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}", text)]
    if not words:
        return f"[{path}] 没有可统计的词"

    counter = Counter(words)
    rows = "\n".join(f"    {n:4d}  {w}" for w, n in counter.most_common(max(1, top)))
    return f"[{path}] 共 {len(words)} 个词 / 去重 {len(counter)} 个\n  词频前 {min(top, len(counter))}：\n{rows}"
