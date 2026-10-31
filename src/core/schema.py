"""
从函数签名自动推导 JSON Schema。

这是「丢一个 .py 文件就多一个工具」的关键：
    工具作者只要写一个带类型注解和 docstring 的普通函数，
    装饰器就能自动生成模型需要的 function-calling schema，
    不需要手写任何 JSON。

支持两种参数描述来源（可混用、以显式 `params=` 为最高优先级）：
    1. Google 风格 docstring 的 Args / 参数 段
    2. Sphinx 风格的 :param name: 描述
"""

from __future__ import annotations

import inspect
import json
import re
import types
import typing

# Python 类型 -> JSON Schema 类型
_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    tuple: "array",
    set: "array",
    dict: "object",
}

# 字符串注解（模块里有 `from __future__ import annotations` 时注解就是字符串）
_STR_TO_JSON = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "tuple": "array",
    "set": "array",
    "dict": "object",
    "List": "array",
    "Tuple": "array",
    "Set": "array",
    "Dict": "object",
    "Sequence": "array",
    "Iterable": "array",
    "Mapping": "object",
    "Any": "string",
}

_ARGS_HEADER = re.compile(r"^(Args|Arguments|Parameters|参数)\s*[:：]\s*$")
_ARGS_END = re.compile(r"^(Returns|Return|Raises|Yields|Examples|Note|返回|异常|示例)\s*[:：]")
_ARG_LINE = re.compile(r"^(\*{0,2}[A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*[:：]\s*(.+)$")
_SPHINX_PARAM = re.compile(r":param\s+(\w+)\s*:\s*(.+)")


def json_type(annotation: object) -> str | None:
    """把 Python 类型注解映射成 JSON Schema 类型；识别不了就返回 None。"""
    if annotation is None or annotation is inspect.Parameter.empty:
        return None

    # 字符串注解：Optional[str] / str | None / List[int] ...
    if isinstance(annotation, str):
        s = annotation.strip()
        # 去掉所有 typing. 前缀（不限于最外层：Optional[typing.List[int]] 的内层也要剥）
        s = re.sub(r"typing\.", "", s)
        s = re.sub(r"\s*\|\s*None\s*$", "", s)      # X | None -> X
        s = re.sub(r"^Optional\[(.*)\]$", r"\1", s)  # Optional[X] -> X
        # 去掉容器参数：List[int] -> List
        s = re.sub(r"\[.*\]$", "", s).strip()
        return _STR_TO_JSON.get(s)

    origin = typing.get_origin(annotation)
    # Optional[X]（typing.Union）与 PEP 604 的 X | None（types.UnionType）都要处理
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return json_type(args[0]) if args else None
    if origin in (list, tuple, set, frozenset):
        return "array"
    if origin is dict:
        return "object"

    if isinstance(annotation, type):
        if annotation in _PY_TO_JSON:
            return _PY_TO_JSON[annotation]
        for py, js in _PY_TO_JSON.items():
            try:
                if issubclass(annotation, py):
                    return js
            except TypeError:
                continue
        if annotation.__name__ in _STR_TO_JSON:
            return _STR_TO_JSON[annotation.__name__]
    return None


def param_docs(func) -> dict[str, str]:
    """从 docstring 里抽取参数说明。"""
    doc = inspect.getdoc(func) or ""
    out: dict[str, str] = {}
    if not doc:
        return out

    in_args = False
    for raw in doc.splitlines():
        line = raw.strip()
        if not in_args:
            if _ARGS_HEADER.match(line):
                in_args = True
            continue
        if _ARGS_END.match(line) or not line:
            if not line:
                continue
            break
        m = _ARG_LINE.match(line)
        if m:
            name = m.group(1).lstrip("*")
            desc = m.group(2).strip()
            # 续行（缩进更深的描述）暂不合并，保持简单可预测
            out[name] = desc

    for m in _SPHINX_PARAM.finditer(doc):
        out.setdefault(m.group(1), m.group(2).strip())
    return out


def summary(func) -> str:
    """取 docstring 的首行作为工具描述。"""
    doc = inspect.getdoc(func) or ""
    for line in doc.splitlines():
        if line.strip():
            return line.strip()
    return func.__name__


def _serializable(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def schema_from_callable(func) -> dict:
    """从函数签名 + docstring 推导出完整的 JSON Schema。"""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "required": []}

    docs = param_docs(func)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, p in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue

        entry: dict = {"type": json_type(p.annotation) or "string"}
        if name in docs:
            entry["description"] = docs[name]

        if p.default is p.empty:
            required.append(name)
        elif _serializable(p.default):
            entry["default"] = p.default

        properties[name] = entry

    return {"type": "object", "properties": properties, "required": required}


def merge_params(auto: dict, override: dict | None, required_override: list | None) -> dict:
    """把显式声明的参数合并到自动推导结果上。

    支持三种写法，按便利性递增：
        params={"path": "string"}                              # 只给类型
        params={"path": {"type": "string", "description": "…"}} # 给类型和描述
        params={"type": "object", "properties": {...}}          # 完整 schema（旧写法）
    """
    if not override:
        result = {
            "type": "object",
            "properties": dict(auto["properties"]),
            "required": list(auto["required"]),
        }
        if required_override is not None:
            result["required"] = list(required_override)
        return result

    # 旧写法：直接给完整 schema —— 除 properties/required 之外的键（title、
    # description、additionalProperties 等）也要原样带上，required_override 优先。
    if override.get("type") == "object" and "properties" in override:
        result = dict(override)
        result["properties"] = dict(override["properties"])
        result["required"] = (
            list(required_override)
            if required_override is not None
            else list(override.get("required", []))
        )
        return result

    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, spec in override.items():
        if isinstance(spec, str):
            spec = {"type": spec}
        properties[name] = dict(spec)
        if not spec.get("has_default") and "default" not in spec:
            if name in auto["required"]:
                required.append(name)

    # 自动推导出、但没被显式覆盖的参数：保留（便于"只覆盖一部分"）
    for name, spec in auto["properties"].items():
        if name not in properties:
            properties[name] = spec
            if name in auto["required"]:
                required.append(name)

    if required_override is not None:
        required = list(required_override)

    return {"type": "object", "properties": properties, "required": required}
