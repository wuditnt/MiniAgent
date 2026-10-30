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
    "列出当前运行的进程（PID / 内存 MB / 进程名），按内存降序。",
    params={
        "keyword": {"type": "string", "description": "按进程名过滤（子串匹配），留空列全部", "default": ""},
        "limit": {"type": "integer", "description": "最多显示条数", "default": 50},
    },
)
def list_processes(keyword: str = "", limit: int = 50) -> str:
    """按内存占用列出进程。

    为什么不用 psutil 列 Windows 进程？实测（331 个进程）：
      psutil.process_iter(['pid','name'])            31.4s
      psutil.process_iter([... ,'memory_info'])      31.8s
      psutil.process_iter([... ,'cmdline'])          81.7s
      PowerShell Get-Process                          0.11s
    本机安全软件让 psutil 的逐进程查询慢了约 100 倍，用户只能干等/手动取消。
    所以 Windows 走 Get-Process（带 15s 超时），失败再退 tasklist；
    psutil 只保留给 Linux/macOS。
    """
    limit = max(1, min(int(limit or 50), 200))
    keyword = (keyword or "").strip().lower()

    rows: list[tuple[int, float, str]] = []
    if sys.platform == "win32":
        rows = _processes_windows()
    if not rows:
        rows = _processes_psutil()

    if keyword:
        rows = [r for r in rows if keyword in r[2].lower()]
    if not rows:
        return "（没有匹配的进程）" if keyword else "[error] 未能获取进程列表"
    rows.sort(key=lambda r: r[1], reverse=True)
    shown = rows[:limit]
    body = "\n".join(f"  {pid:7d}  {mem:8.1f} MB  {name}" for pid, mem, name in shown)
    more = len(rows) - len(shown)
    tail = f"\n…（还有 {more} 条未显示）" if more > 0 else ""
    return f"共 {len(rows)} 个进程，按内存排序显示前 {len(shown)}：\n{body}{tail}"


def _processes_windows() -> list[tuple[int, float, str]]:
    """Windows 快速路径：Get-Process（约 0.1s），失败退 tasklist（约 1.6s）。"""
    from core import run_process

    ps = (
        "Get-Process | ForEach-Object { "
        "'{0}|{1}|{2}' -f $_.Id, $_.ProcessName, [math]::Round($_.WS/1MB,1) }"
    )
    try:
        code, out, _err, _c = run_process(
            ["powershell", "-NoProfile", "-Command", ps], timeout=15
        )
    except TimeoutError:
        return []
    if code == 0:
        rows = []
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            try:
                rows.append((int(parts[0]), float(parts[2]), parts[1]))
            except ValueError:
                continue
        if rows:
            return rows

    # 退路：tasklist（老系统上 PowerShell 被策略限制时的保险）
    try:
        code, out, _err, _c = run_process("tasklist /FO CSV /NH", shell=True, timeout=15)
    except TimeoutError:
        return []
    if code != 0:
        return []
    import csv
    import io

    rows = []
    for rec in csv.reader(io.StringIO(out)):
        if len(rec) < 5:
            continue
        try:
            mem_kb = float(rec[4].replace(",", "").replace(" K", "").replace("K", "").strip())
            rows.append((int(rec[1]), mem_kb / 1024, rec[0]))
        except ValueError:
            continue
    return rows


def _processes_psutil() -> list[tuple[int, float, str]]:
    """POSIX 路径（psutil 在 Linux/macOS 上很快）；没装 psutil 时退到 ps。"""
    try:
        import psutil  # type: ignore
    except ImportError:
        return _processes_ps()
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = p.info
            mem = (info["memory_info"].rss / 1024**2) if info.get("memory_info") else 0
            rows.append((info["pid"], mem, info.get("name", "?")))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def _processes_ps() -> list[tuple[int, float, str]]:
    from core import run_process

    try:
        code, out, _err, _c = run_process(
            "ps -eo pid,rss,comm --sort=-rss", shell=True, timeout=10
        )
    except TimeoutError:
        return []
    if code != 0:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), float(parts[1]) / 1024, parts[2].strip()))
        except ValueError:
            continue
    return rows


@tool(
    "env_info",
    "查看与 mini-agent 运行相关的环境变量（不打印密钥值本身）。",
)
def env_info() -> str:
    keys = [
        "MINI_AGENT_HOST", "MINI_AGENT_PORT",
        "MINI_AGENT_WORKSPACE", "MINI_AGENT_NO_PLUGINS", "MINI_AGENT_PLUGIN_DIRS",
        "MINI_AGENT_NO_SKILLS", "MINI_AGENT_SKILL_DIRS", "MINI_AGENT_STREAM",
        "MINI_AGENT_PARALLEL", "MINI_AGENT_MAX_WORKERS", "MINI_AGENT_MAX_OUTPUT",
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
