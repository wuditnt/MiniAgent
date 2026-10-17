"""
系统信息工具组。

都是只读的，帮助模型「了解自己在什么环境里」。
Windows 下子进程、磁盘、进程信息全有；Unix 下自动退化成可用部分。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys

from core import tool, workspace

PLUGIN_INFO = {"name": "系统信息", "version": "1.0", "author": "mini-agent"}


@tool(
    "system_info",
    "查看运行环境的总体信息：操作系统、Python、工作区、磁盘、已装关键包。",
)
def system_info() -> str:
    ws = workspace()
    disk = shutil.disk_usage(ws)
    total_gb = disk.total / 1024**3
    free_gb = disk.free / 1024**3

    # 关键依赖是否就位（不强制 import，缺了就标缺失）
    deps = {}
    for pkg in ("openai", "rich", "prompt_toolkit"):
        try:
            m = __import__(pkg)
            deps[pkg] = getattr(m, "__version__", "已装")
        except ImportError:
            deps[pkg] = "未安装"

    lines = [
        f"操作系统 : {platform.system()} {platform.release()}（{platform.machine()}）",
        f"Python   : {platform.python_version()}（{sys.executable}）",
        f"平台     : {sys.platform}",
        f"工作区   : {ws}",
        f"磁盘空间 : {free_gb:.1f} GB 可用 / {total_gb:.1f} GB 总计",
        f"逻辑 CPU : {os.cpu_count() or '未知'} 核",
        f"PID      : {os.getpid()}",
        "",
        "关键依赖:",
    ]
    for k, v in deps.items():
        lines.append(f"  {k:20s} {v}")
    return "\n".join(lines)


@tool(
    "disk_usage",
    "查看指定目录所在磁盘的用量（总量/已用/可用）。",
    params={"path": {"type": "string", "description": "目录路径，留空用工作区", "default": "."}},
)
def disk_usage(path: str = ".") -> str:
    from core import read_path

    ap = read_path(path)
    if not os.path.exists(ap):
        return f"[error] 路径不存在：{ap}"
    d = shutil.disk_usage(ap)
    used_gb = (d.total - d.free) / 1024**3
    total_gb = d.total / 1024**3
    free_gb = d.free / 1024**3
    pct = (d.total - d.free) / d.total * 100
    return (
        f"[{ap}]\n"
        f"  总量: {total_gb:.2f} GB\n"
        f"  已用: {used_gb:.2f} GB ({pct:.1f}%)\n"
        f"  可用: {free_gb:.2f} GB"
    )


@tool(
    "list_processes",
    "列出当前运行的进程（PID / 内存 MB / 命令行）。Windows 优先用 tasklist。",
    params={
        "keyword": {"type": "string", "description": "按命令行过滤（子串匹配），留空列全部", "default": ""},
        "limit": {"type": "integer", "description": "最多显示条数", "default": 50},
    },
)
def list_processes(keyword: str = "", limit: int = 50) -> str:
    # 优先用 psutil（如果装了），信息更全；否则退到原生方式
    try:
        import psutil  # type: ignore

        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cmdline"]):
            try:
                info = p.info
                cmd = " ".join(info.get("cmdline") or [])
                if keyword and keyword.lower() not in cmd.lower() and keyword not in info.get("name", ""):
                    continue
                mem = (info["memory_info"].rss / 1024**2) if info.get("memory_info") else 0
                rows.append((info["pid"], mem, info.get("name", "?"), cmd))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda r: r[1], reverse=True)
        shown = rows[:limit]
        body = "\n".join(f"  {pid:6d}  {mem:8.1f} MB  {name}  {cmd[:80]}" for pid, mem, name, cmd in shown)
        more = len(rows) - len(shown)
        tail = f"\n…（还有 {more} 条未显示）" if more > 0 else ""
        return f"共 {len(rows)} 个进程，按内存排序显示前 {len(shown)}：\n{body}{tail}"
    except ImportError:
        pass

    # 退回方案：用 run_process 跑 tasklist / ps
    from core import run_process

    if sys.platform == "win32":
        code, out, err, _ = run_process("tasklist /FO CSV /NH", shell=True, timeout=10)
    else:
        code, out, err, _ = run_process("ps -eo pid,rss,comm,args --sort=-rss", shell=True, timeout=10)
    if code != 0:
        return f"[error] 进程列表获取失败：{err or out}"
    if keyword:
        out = "\n".join(l for l in out.splitlines() if keyword.lower() in l.lower())
    lines = out.splitlines()[:limit]
    return "\n".join(lines) or "（没有匹配的进程）"


@tool(
    "env_info",
    "查看与 mini-agent 运行相关的环境变量（不打印密钥值本身）。",
)
def env_info() -> str:
    keys = [
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
        "AGENT_WORKSPACE", "AGENT_NO_PLUGINS", "AGENT_PLUGIN_DIRS",
        "AGENT_NO_SKILLS", "AGENT_SKILL_DIRS", "AGENT_STREAM",
        "AGENT_PARALLEL", "AGENT_MAX_WORKERS", "AGENT_MAX_OUTPUT",
    ]
    out = []
    for k in keys:
        v = os.environ.get(k)
        if v is None:
            out.append(f"  {k:22s} (未设置)")
        elif "KEY" in k:
            out.append(f"  {k:22s} *** (已设置，{len(v)} 字符)")
        else:
            out.append(f"  {k:22s} {v}")
    return "mini-agent 相关环境变量:\n" + "\n".join(out)
