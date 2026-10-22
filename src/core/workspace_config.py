"""
工作区配置 —— 会话的「容器」，同时决定 Agent 的文件操作边界（沙箱根目录）。

为什么单独一个模块？
  · sandbox.py 只管「当前边界在哪」，但不关心「有哪些边界、叫什么名字」；
    这份「用户配置的多个工作区」需要独立持久化，且必须可热切换。
  · 会话归属工作区：每个会话文件里记一个 workspace_id，切到该会话时
    把 sandbox 的工作区切过去，工具产生的相对路径就落在对的目录下。

存储：<数据目录>/workspaces.json（本机文件，不进版本控制）。
结构：{"workspaces": [{id, name, path, created_at}]}
「默认工作区」始终存在且不可删除，path 由系统管理、固定指向用户数据目录
（`DEFAULT_WORKSPACE`，如 ~/mini-agent），加载时会被校正回来。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from ._paths import DEFAULT_WORKSPACE, ROOT, data_dir

_WORKSPACES_FILE = os.path.join(data_dir(), "workspaces.json")

#: 默认工作区 id —— 老会话（没有 workspace_id 字段）都归到它名下
DEFAULT_ID = "default"


def _default_path() -> str:
    """默认工作区的路径 —— 系统管理，始终指向用户数据目录。"""
    return os.path.abspath(DEFAULT_WORKSPACE)


def _default_item() -> dict:
    return {
        "id": DEFAULT_ID,
        "name": "默认工作区",
        "path": _default_path(),
        "created_at": time.time(),
    }


def ensure_default_workspace() -> str:
    """幂等地确保默认工作区目录存在，返回其绝对路径。

    在进程启动时调用一次即可：Web 后端、控制台、--selftest 都拿这个目录
    当工具的工作目录（cwd / 沙箱根），先建好能避免「第一次落盘才发现目录不在」。
    建不出来（权限等）不应阻断启动 —— 真正用到时自然会在工具层报出明确错误。
    """
    path = _default_path()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _load_raw() -> dict:
    """读配置文件；不存在或损坏时回退到「只有默认工作区」。"""
    if not os.path.exists(_WORKSPACES_FILE):
        return {"workspaces": [_default_item()]}
    try:
        with open(_WORKSPACES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"workspaces": [_default_item()]}
    if not isinstance(data, dict) or not isinstance(data.get("workspaces"), list):
        return {"workspaces": [_default_item()]}
    return data


def _save_raw(data: dict) -> None:
    """原子写：先写临时文件再 rename，避免半截 JSON。"""
    os.makedirs(os.path.dirname(_WORKSPACES_FILE), exist_ok=True)
    tmp = _WORKSPACES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _WORKSPACES_FILE)


def _ensure_default(data: dict) -> dict:
    """兜底 + 校正：默认工作区必须存在，且 path 始终指向用户数据目录。

    老版本把默认工作区锚在项目根（源码目录），加载时这里会把它的 path
    校正到 `DEFAULT_WORKSPACE` —— 相当于一次隐式迁移，无需手工改 JSON。
    """
    items = data.setdefault("workspaces", [])
    for w in items:
        if isinstance(w, dict) and w.get("id") == DEFAULT_ID:
            w["path"] = _default_path()
            if not w.get("name"):
                w["name"] = "默认工作区"
            return data
    items.insert(0, _default_item())
    return data


def list_workspaces() -> list[dict]:
    """返回全部工作区（默认项保证存在）。"""
    data = _ensure_default(_load_raw())
    out = []
    for w in data["workspaces"]:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        out.append({
            "id": w["id"],
            "name": w.get("name") or "未命名工作区",
            "path": w.get("path") or ROOT,
            "created_at": w.get("created_at", 0),
        })
    return out


def get_workspace(wid: str) -> dict | None:
    for w in list_workspaces():
        if w["id"] == wid:
            return w
    return None


def resolve_path(wid: str) -> str:
    """把 workspace_id 解析成沙箱根目录；找不到就回退到默认工作区（用户数据目录）。"""
    w = get_workspace(wid)
    return os.path.abspath(w["path"]) if w else _default_path()


def _validate_path(path: str) -> str:
    """校验并规范化路径：必须是非空、存在的目录。"""
    p = os.path.abspath(os.path.expanduser((path or "").strip()))
    if not p or not os.path.isdir(p):
        raise ValueError(f"工作区路径不存在或不是目录：{path}")
    return p


def create_workspace(name: str, path: str) -> dict:
    """新增工作区。name 必填；path 必须是存在的目录，且不与现有工作区重复。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("工作区名称不能为空")
    p = _validate_path(path)
    data = _ensure_default(_load_raw())
    for w in data["workspaces"]:
        if os.path.normcase(os.path.abspath(w.get("path", ""))) == os.path.normcase(p):
            raise ValueError("该路径已被另一个工作区使用")
    item = {"id": uuid.uuid4().hex[:12], "name": name, "path": p, "created_at": time.time()}
    data["workspaces"].append(item)
    _save_raw(data)
    return item


def update_workspace(wid: str, name: str | None = None, path: str | None = None) -> dict:
    """改名 / 改路径。默认工作区的路径不建议改（会被沙箱锚定），这里仍允许改名。"""
    data = _ensure_default(_load_raw())
    target = None
    for w in data["workspaces"]:
        if w.get("id") == wid:
            target = w
            break
    if target is None:
        raise ValueError("工作区不存在")

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("工作区名称不能为空")
        target["name"] = name

    if path is not None:
        p = _validate_path(path)
        for w in data["workspaces"]:
            if w.get("id") == wid:
                continue
            if os.path.normcase(os.path.abspath(w.get("path", ""))) == os.path.normcase(p):
                raise ValueError("该路径已被另一个工作区使用")
        target["path"] = p

    _save_raw(data)
    return target


def delete_workspace(wid: str) -> None:
    """删除工作区。默认工作区不可删；非空由 API 层拦截（需先清空其会话）。"""
    if wid == DEFAULT_ID:
        raise ValueError("默认工作区不可删除")
    data = _ensure_default(_load_raw())
    before = len(data["workspaces"])
    data["workspaces"] = [w for w in data["workspaces"] if w.get("id") != wid]
    if len(data["workspaces"]) == before:
        raise ValueError("工作区不存在")
    _save_raw(data)
