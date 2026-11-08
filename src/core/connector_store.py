"""MCP 连接器的用户级配置存储。

为什么不能继续读安装目录里的 mcp/mcp_servers.json：
桌面版把应用文件放在 resources/app 下，安装到 Program Files 后是只读的，
用户在界面上加一个连接器就会写失败。这里统一落到 ~/.mini-agent/connectors.json，
首次启动时从旧的 mcp/mcp_servers.json 迁移一次（只补缺失，不覆盖用户已有配置）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading

from core._paths import ROOT, data_dir

CONFIG_NAME = "connectors.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PY_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?(?:\.exe)?$|^py(?:\.exe)?$", re.IGNORECASE)
_MAX_TEXT = 2000
_MAX_ARGS = 64
_MAX_ENV = 64

_lock = threading.RLock()


class ConnectorError(ValueError):
    """配置不合法（直接展示给用户，所以消息要写人话）。"""


def config_path() -> str:
    return os.path.join(data_dir(), CONFIG_NAME)


def _legacy_path() -> str:
    return os.path.join(ROOT, "mcp", "mcp_servers.json")


def _empty() -> dict:
    # defaults.connectors = None 表示「未设置」：新建会话继承全部已启用连接器。
    # 空列表则是用户显式选择「一个都不勾」。
    return {"version": 1, "servers": {}, "defaults": {"connectors": None}}


def _text(value, field: str, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConnectorError(f"{field} 必须是字符串")
    value = value.strip()
    if len(value) > limit:
        raise ConnectorError(f"{field} 太长（最多 {limit} 字符）")
    return value


def normalize(name: str, spec: dict) -> dict:
    """校验并补默认值。name 是连接器的稳定 id，也是工具命名空间。"""
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        raise ConnectorError("标识只能包含字母、数字、下划线或短横线，长度 1-40")
    if not isinstance(spec, dict):
        raise ConnectorError("连接器配置必须是对象")

    transport = str(spec.get("transport") or "stdio").strip().lower()
    if transport != "stdio":
        raise ConnectorError("当前版本只支持 stdio 连接器")

    command = _text(spec.get("command"), "启动命令", 512)
    if not command:
        raise ConnectorError("启动命令不能为空")

    raw_args = spec.get("args") or []
    if isinstance(raw_args, str):
        raw_args = [line.strip() for line in raw_args.splitlines() if line.strip()]
    if not isinstance(raw_args, list) or len(raw_args) > _MAX_ARGS:
        raise ConnectorError(f"参数必须是字符串列表（最多 {_MAX_ARGS} 项）")
    args = [_text(a, "参数", 512) for a in raw_args]
    args = [a for a in args if a]

    raw_env = spec.get("env") or {}
    if not isinstance(raw_env, dict) or len(raw_env) > _MAX_ENV:
        raise ConnectorError(f"环境变量必须是键值对象（最多 {_MAX_ENV} 项）")
    env: dict[str, str] = {}
    for k, v in raw_env.items():
        key = _text(k, "环境变量名", 128)
        if not key:
            continue
        env[key] = _text(v, f"环境变量 {key}", 4000)

    raw_timeout = spec.get("timeout", 30)
    if raw_timeout is None or raw_timeout == "":
        raw_timeout = 30
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as e:
        raise ConnectorError("超时必须是数字") from e
    if not 1 <= timeout <= 600:
        raise ConnectorError("超时范围是 1-600 秒")

    return {
        "display_name": _text(spec.get("display_name"), "显示名称", 80) or name,
        "description": _text(spec.get("description"), "说明"),
        "transport": "stdio",
        "command": command,
        "args": args,
        "cwd": _text(spec.get("cwd"), "工作目录", 1024),
        "env": env,
        "timeout": timeout,
        "enabled": bool(spec.get("enabled", True)),
        "dangerous": bool(spec.get("dangerous", True)),
    }


def _read_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConnectorError("配置文件根节点必须是对象")
    return data


def _normalize_all(raw: dict) -> dict:
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        raise ConnectorError('配置格式不对，应形如 {"servers": {...}}')
    out: dict[str, dict] = {}
    for name, spec in servers.items():
        try:
            out[str(name)] = normalize(str(name), spec)
        except ConnectorError:
            # 旧配置里可能有不合规的条目：保留给用户看不如直接跳过，
            # 否则一个坏条目会让整个连接器页面打不开。
            continue
    return {"version": 1, "servers": out, "defaults": _normalize_defaults(raw, out)}


def _normalize_defaults(raw: dict, servers: dict[str, dict]) -> dict:
    """规范化全局默认勾选；缺失/非法一律视为「未设置」（None）。"""
    defaults = raw.get("defaults") if isinstance(raw, dict) else None
    names = defaults.get("connectors") if isinstance(defaults, dict) else None
    if not isinstance(names, list):
        return {"connectors": None}
    out: list[str] = []
    for name in names:
        name = str(name).strip()
        # 连接器已被删除时顺手清掉，避免默认勾选里留着不存在的名字
        if name in servers and name not in out:
            out.append(name)
    return {"connectors": out}


def load() -> dict:
    """读取配置；首次运行时从安装目录的旧配置迁移。"""
    with _lock:
        path = config_path()
        if os.path.isfile(path):
            try:
                return _normalize_all(_read_file(path))
            except (OSError, json.JSONDecodeError, ConnectorError):
                # 配置坏了就回空表，不阻塞启动；用户可以在界面上重新添加。
                return _empty()

        data = _empty()
        legacy = _legacy_path()
        if os.path.isfile(legacy):
            try:
                data = _normalize_all(_read_file(legacy))
            except (OSError, json.JSONDecodeError, ConnectorError):
                data = _empty()
        if data["servers"]:
            try:
                _write(data)
            except OSError:
                pass
        return data


def _write(data: dict) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save(data: dict) -> dict:
    with _lock:
        clean = _normalize_all(data)
        _write(clean)
        return clean


def upsert(name: str, spec: dict) -> dict:
    with _lock:
        data = load()
        data["servers"][str(name)] = normalize(str(name), spec)
        _write(data)
        return data["servers"][str(name)]


def remove(name: str) -> bool:
    with _lock:
        data = load()
        existed = data["servers"].pop(str(name), None) is not None
        if existed:
            # 默认勾选里也顺手摘掉，避免别处读到已删除的名字
            names = data["defaults"]["connectors"]
            if isinstance(names, list) and name in names:
                data["defaults"]["connectors"] = [n for n in names if n != name]
            _write(data)
        return existed


def set_enabled(name: str, enabled: bool) -> dict:
    with _lock:
        data = load()
        if name not in data["servers"]:
            raise ConnectorError("连接器不存在")
        data["servers"][name]["enabled"] = bool(enabled)
        _write(data)
        return data["servers"][name]


def get_defaults() -> list[str] | None:
    """全局默认勾选：None = 未设置（新建会话继承全部已启用项）。"""
    return load()["defaults"]["connectors"]


def set_defaults(names: list[str]) -> list[str]:
    """写入全局默认勾选（只保留真实存在的连接器，顺序按传入顺序）。"""
    with _lock:
        data = load()
        out: list[str] = []
        for name in names:
            name = str(name).strip()
            if name in data["servers"] and name not in out:
                out.append(name)
        data["defaults"]["connectors"] = out
        _write(data)
        return out


def resolve_selection(selected: list[str] | None) -> list[str]:
    """把「会话勾选」解析成本轮实际可用的连接器名。

    - selected is None：跟全局默认；默认也没设置就继承全部已启用项；
    - 结果再与当前 enabled 求交集，禁用/已删除的连接器自动失效。
    """
    data = load()
    enabled = [n for n, spec in data["servers"].items() if spec.get("enabled", True)]
    if selected is None:
        pool = data["defaults"]["connectors"]
        if pool is None:
            pool = enabled
    else:
        pool = selected
    return [n for n in pool if n in enabled]


# ---------------------------------------------------------------- 运行环境

def _which(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def _node_candidates() -> list[str]:
    if os.name != "nt":
        return []
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    out = []
    for r in roots:
        if r:
            out.append(os.path.join(r, "nodejs", "node.exe"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        out.append(os.path.join(local, "Programs", "nodejs", "node.exe"))
    return out


def _first_existing(paths: list[str]) -> str:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return ""


def _version(exe: str) -> str:
    if not exe:
        return ""
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            timeout=5, encoding="utf-8", errors="replace", **kwargs,
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def command_available(command: str) -> bool:
    """界面提示用：这条启动命令现在能不能找到。"""
    cmd = str(command or "").strip().strip('"')
    if not cmd:
        return False
    if os.path.dirname(cmd):
        return os.path.exists(cmd)
    # MCP 会把 python/py 改写成应用自带的解释器，所以它们永远可用
    if _PY_RE.match(cmd):
        return True
    return shutil.which(cmd) is not None


def runtime_info() -> dict:
    node = _which("node") or _first_existing(_node_candidates())
    npx = _which("npx") or _first_existing([
        os.path.join(os.environ.get("APPDATA", ""), "npm", "npx.cmd")
    ])
    return {
        "node": {"found": bool(node), "path": node, "version": _version(node)},
        "npx": {"found": bool(npx), "path": npx},
    }
