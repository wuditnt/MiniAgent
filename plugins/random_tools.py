"""
随机与标识工具组。

gen_uuid        生成 UUID（v4）
random_string   生成指定长度的随机字符串
random_int      给定区间随机整数
random_choice   从列表里随机选一个
"""

from __future__ import annotations

import random
import string
import uuid

from core import tool

PLUGIN_INFO = {"name": "随机与标识", "version": "1.0", "author": "mini-agent"}


@tool(
    "gen_uuid",
    "生成 UUID（默认 v4）。",
    params={
        "count": {"type": "integer", "description": "生成数量", "default": 1},
        "upper": {"type": "boolean", "description": "是否大写", "default": False},
    },
)
def gen_uuid(count: int = 1, upper: bool = False) -> str:
    ids = [str(uuid.uuid4()) for _ in range(max(1, min(count, 1000)))]
    if upper:
        ids = [s.upper() for s in ids]
    return "\n".join(ids)


@tool(
    "random_string",
    "生成指定长度的随机字符串。",
    params={
        "length": {"type": "integer", "description": "字符串长度", "default": 16},
        "charset": {"type": "string", "description": "字符集：alphanum / alpha / digits / hex / all", "default": "alphanum"},
        "count": {"type": "integer", "description": "生成数量", "default": 1},
    },
)
def random_string(length: int = 16, charset: str = "alphanum", count: int = 1) -> str:
    pools = {
        "alphanum": string.ascii_letters + string.digits,
        "alpha": string.ascii_letters,
        "digits": string.digits,
        "hex": string.hexdigits,
        "all": string.ascii_letters + string.digits + string.punctuation,
    }
    pool = pools.get(charset, pools["alphanum"])
    length = max(1, min(length, 1000))
    count = max(1, min(count, 1000))
    out = ["".join(random.choices(pool, k=length)) for _ in range(count)]
    return "\n".join(out)


@tool(
    "random_int",
    "生成 [min, max] 区间内的随机整数。",
    params={
        "min": {"type": "integer", "description": "最小值", "default": 0},
        "max": {"type": "integer", "description": "最大值", "default": 100},
        "count": {"type": "integer", "description": "生成数量", "default": 1},
    },
)
def random_int(min: int = 0, max: int = 100, count: int = 1) -> str:
    if min > max:
        min, max = max, min
    count = max(1, min(count, 1000))
    out = [str(random.randint(min, max)) for _ in range(count)]
    return "\n".join(out)


@tool(
    "random_choice",
    "从给定的列表里随机选一个（或多个）。",
    params={
        "items": {"type": "array", "items": {"type": "string"}, "description": "候选列表"},
        "count": {"type": "integer", "description": "选几个", "default": 1},
    },
    required=["items"],
)
def random_choice(items: list[str], count: int = 1) -> str:
    if not items:
        return "[error] 候选列表为空"
    count = max(1, min(count, len(items)))
    chosen = random.sample(items, count)
    return "\n".join(chosen)
