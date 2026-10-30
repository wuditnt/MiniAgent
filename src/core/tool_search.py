"""
工具检索：standard 档位下的「按需加载」。

standard 档位的会话工具表只常驻常用分组（文件 / 文档 / 代码 / 数据 / 联网 / 系统基础），
低频领域工具（OCR、压缩包、数据库、DataFrame、系统监控、网络诊断、随机值、哈希编码、
图片转换、时间计算、文本处理）不进 schema —— 每轮省下约 1.5 万字符。

但「没常驻」不等于「不能用」：模型遇到这类需求时先调用本工具检索，命中的工具会被加进
**当前会话**的工具表，下一轮请求里就是正常声明的工具，直接调用即可。

为什么不全量塞：工具 schema 每轮都随请求发送，95 个工具约 34k 字符，而单个任务通常
只用到其中个位数 —— 全量加载等于每轮都为用不到的描述付费。

为什么不做向量检索：本地单机、工具不足百个，名字 + 分组 + 描述的关键词与二元组匹配
已经够用，且零依赖、可解释（命中理由能直接说给模型听）。
"""

from __future__ import annotations

import re

from .registry import REGISTRY, Tool, ToolRegistry, current_registry

#: standard 档位不常驻（但可检索加载）的低频分组。
#: 判断依据：这些分组平均每次任务都很少被选，但 schema 合计约 1.5 万字符。
STANDARD_HIDDEN_GROUPS = frozenset({
    "sys_monitor",   # cpu / 磁盘 / 网络速率 / 进程排行
    "net_extra",     # ping / dns / check_port / http_post
    "random",        # random_int / random_string / gen_uuid / random_choice
    "hash",          # base64 / 文本哈希
    "sys_extra",     # 剪贴板 / kill_process / wait_for_file
    "dataframe",     # df_sql / df_summary / df_to_parquet
    "database",      # sqlite / csv_sql
    "archive",       # zip 创建与解包
    "ocr",           # ocr_image / ocr_pdf
    "image_meta",    # image_convert / image_info
    "time_math",     # time_add / time_diff
    "text_extra",    # case_convert / url 编解码 / json_query / html_to_text
})

#: 分组 → 中文名。检索没命中时用于告诉模型「能搜什么」，也参与打分（中文查询靠它兜底）。
_GROUP_LABELS: dict[str, str] = {
    "sys_monitor": "系统监控",
    "net_extra": "网络诊断",
    "random": "随机值",
    "hash": "编码与哈希",
    "sys_extra": "剪贴板与进程",
    "dataframe": "DataFrame 分析",
    "database": "数据库查询",
    "archive": "压缩包",
    "ocr": "OCR 文字识别",
    "image_meta": "图片转换",
    "time_math": "时间计算",
    "text_extra": "文本处理",
}

_SPLIT = re.compile(r"[\s,，。、;；:：!?！？()（）\[\]【】\"'`]+")
_MAX_LIMIT = 10
_DESC_CHARS = 80


def _terms(query: str) -> list[str]:
    """查询切词：先按空白/标点切开，中文长词再补二元组。

    中文没有空格，整句「识别图片里的文字」在工具描述里几乎不可能原样出现；
    拆成「识别 / 图片 / 文字」这类二元组才能命中 —— 这是最便宜、且无依赖的做法。
    """
    out: list[str] = []
    for chunk in _SPLIT.split((query or "").strip().lower()):
        if not chunk:
            continue
        out.append(chunk)
        if len(chunk) > 2 and not chunk.isascii():
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    seen: set[str] = set()
    return [t for t in out if t and not (t in seen or seen.add(t))]


def _score(tool: Tool, terms: list[str], phrase: str) -> int:
    """命中打分：整句命中 > 名字 > 分组 > 描述。只用于排序，不追求精确。"""
    name = tool.name.lower()
    group = tool.group.lower()
    label = _GROUP_LABELS.get(tool.group, "").lower()
    desc = (tool.description or "").lower()
    hay = f"{name}\n{group}\n{label}\n{desc}"
    score = 8 if phrase and phrase in hay else 0
    for term in terms:
        if term in name:
            score += 3
        elif term in group or term in label:
            score += 2
        elif term in desc:
            score += 1
    return score


def _brief(text: str, limit: int = _DESC_CHARS) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[:limit] + "…"


def _params(tool: Tool) -> str:
    """参数签名：required 用 * 标记，一眼看出哪些必填。"""
    props = (tool.parameters or {}).get("properties") or {}
    required = set((tool.parameters or {}).get("required") or [])
    if not props:
        return "()"
    return "(" + ", ".join(p + ("*" if p in required else "") for p in props) + ")"


def _overview() -> str:
    labels = "、".join(_GROUP_LABELS[g] for g in sorted(STANDARD_HIDDEN_GROUPS, key=lambda g: _GROUP_LABELS.get(g, g)))
    return (
        "[ok] 工具列表只常驻了常用工具。可检索的低频能力："
        f"{labels}。"
        "请用简短关键词再调用一次，例如「识别图片文字」「解压 zip」「sqlite 查询」「cpu 占用」。"
    )


def search_tools(query: str = "", limit: int = 5) -> str:
    """按关键词检索低频工具，并把命中的工具加载进当前会话工具表。"""
    reg = current_registry()
    pool = [
        t for t in REGISTRY.all()
        if t.group in STANDARD_HIDDEN_GROUPS and reg.get(t.name) is None
    ]
    if not pool:
        return "[ok] 当前工具表已包含全部可检索工具，直接调用即可。"

    phrase = (query or "").strip().lower()
    if not phrase:
        return _overview()

    try:
        want = max(1, min(_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        want = 5

    terms = _terms(phrase)
    scored = [(_score(t, terms, phrase), t) for t in pool]
    hits = sorted(
        [(s, t) for s, t in scored if s > 0],
        key=lambda x: (-x[0], x[1].name),
    )[:want]
    if not hits:
        return _overview()

    lines: list[str] = []
    for _score_value, tool in hits:
        if reg.get(tool.name) is None:
            try:
                reg.add(tool)      # 只进本会话表，不影响全局与其他会话
            except ValueError:
                pass               # 并发下已被别的调用加进来：按已加载处理
        lines.append(f"· {tool.name}{_params(tool)} —— {_brief(tool.description)}")
    more = len(pool) - len(hits)
    tail = f"\n（还有 {more} 个可检索工具，需要时换关键词再搜）" if more > 0 and len(hits) == want else ""
    return (
        "[ok] 已加载到本会话，下一轮可直接调用：\n"
        + "\n".join(lines)
        + tail
    )


_TOOL_SEARCH_TOOL = Tool(
    name="search_tools",
    description=(
        "按关键词检索并加载低频工具。工具列表默认只常驻常用工具，"
        "遇到这些能力时先用本工具检索，不要直接回答做不到："
        "OCR/扫描件文字识别、压缩包解压与打包、SQLite/CSV 的 SQL 查询、DataFrame 分析、"
        "系统监控（CPU/内存/磁盘/进程）、网络诊断（ping/DNS/端口）、随机值、"
        "base64/哈希编码、剪贴板与结束进程、图片格式转换、时间加减、"
        "大小写/URL 编解码/JSON 查询/HTML 转文本。"
        "命中后工具会加入本会话，下一轮即可直接调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "简短关键词，如「识别图片文字」「解压 zip」「sqlite 查询」「cpu 占用」",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回几个工具，默认 5（上限 10）",
            },
        },
        "required": ["query"],
    },
    func=search_tools,
    group="核心",
    dangerous=False,
    source="core",
    module="core.tool_search",
)


def register(reg: ToolRegistry | None = None) -> None:
    """把 search_tools 注册进指定注册表（默认全局 REGISTRY）。幂等。"""
    target = reg if reg is not None else REGISTRY
    if target.get("search_tools") is not None:
        return
    target.add(_TOOL_SEARCH_TOOL)
