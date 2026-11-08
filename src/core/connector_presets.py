"""内置 MCP 连接器模板。

模板随包发布（mcp/presets.json），只做固定清单，不拉在线目录 ——
离线可用、版本可控，也避免在用户机器上访问未知来源的配置。

模板只负责「预填编辑表单」：路径类占位符需要用户自己替换，
保存前仍要走一次试连，和自定义连接器完全同一条校验链路。
"""

from __future__ import annotations

import json
import os
import threading

from core._paths import ROOT

PRESETS_PATH = os.path.join(ROOT, "mcp", "presets.json")

_lock = threading.RLock()
_cache: list[dict] | None = None


def _normalize(raw: dict) -> list[dict]:
    presets: list[dict] = []
    items = raw.get("presets") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return presets
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        command = str(item.get("command") or "").strip()
        if not key or not command:
            continue
        args = [
            str(a) for a in (item.get("args") or [])
            if isinstance(a, (str, int, float))
        ]
        presets.append({
            "key": key,
            "display_name": str(item.get("display_name") or key),
            "description": str(item.get("description") or ""),
            "command": command,
            "args": args,
            "dangerous": bool(item.get("dangerous", True)),
            "hint": str(item.get("hint") or ""),
            "docs": str(item.get("docs") or ""),
        })
    return presets


def all() -> list[dict]:
    """全部模板（只读副本，调用方随意修改不影响缓存）。"""
    global _cache
    with _lock:
        if _cache is None:
            try:
                with open(PRESETS_PATH, encoding="utf-8") as f:
                    _cache = _normalize(json.load(f))
            except (OSError, json.JSONDecodeError):
                # 模板文件缺失/损坏不该拖垮连接器页面：返回空清单即可
                _cache = []
        return [dict(p) for p in _cache]


def get(key: str) -> dict | None:
    for item in all():
        if item["key"] == key:
            return item
    return None
