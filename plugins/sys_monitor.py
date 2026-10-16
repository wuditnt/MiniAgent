"""
系统监控工具组：本机运行状态（CPU / 内存 / 磁盘 / 网络 / 开机时长）。

全部只读，实现只用 Python 标准库：
  Windows 走 ctypes 调系统 API（GetSystemTimes / GlobalMemoryStatusEx /
  GetIfTable / GetTickCount64），Linux 读 /proc。
top_cpu_processes 是唯一的可选增强：装了 psutil 才有，未装返回提示
（与 list_processes 同一模式）。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import string
import sys
import time

from core import tool

PLUGIN_INFO = {"name": "系统监控", "version": "1.0", "author": "mini-agent"}

_IS_WIN = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 底层采样：各平台返回统一的 (idle, total) 或计数器元组，供上层算差值
# ---------------------------------------------------------------------------

if _IS_WIN:

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    def _ft64(ft: "_FILETIME") -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def _cpu_times() -> tuple[int, int]:
        """返回 (空闲, 总) 嘀嗒数；总 = kernel + user（kernel 已含 idle）。"""
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError("GetSystemTimes 调用失败")
        return _ft64(idle), _ft64(kernel) + _ft64(user)

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    def _memory() -> dict:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            raise OSError("GlobalMemoryStatusEx 调用失败")
        return {
            "load_pct": st.dwMemoryLoad,
            "total": st.ullTotalPhys,
            "avail": st.ullAvailPhys,
            "page_total": st.ullTotalPageFile,
            "page_avail": st.ullAvailPageFile,
        }

    # MIB_IFROW：只关心入口处的网卡名和收/发字节计数
    _MAX_IF_NAME_LEN = 256
    _MAX_PHYSADDR_LEN = 8
    _MAX_INTERFACE_DESCRIPTION_LEN = 256

    class _MIB_IFROW(ctypes.Structure):
        _fields_ = [
            ("wszName", ctypes.c_wchar * _MAX_IF_NAME_LEN),
            ("dwIndex", ctypes.c_uint32),
            ("dwType", ctypes.c_uint32),
            ("dwMtu", ctypes.c_uint32),
            ("dwSpeed", ctypes.c_uint32),
            ("dwPhysAddrLen", ctypes.c_uint32),
            ("bPhysAddr", ctypes.c_ubyte * _MAX_PHYSADDR_LEN),
            ("dwAdminStatus", ctypes.c_uint32),
            ("dwOperStatus", ctypes.c_uint32),
            ("dwLastChange", ctypes.c_uint32),
            ("dwInOctets", ctypes.c_uint32),
            ("dwInNUcastPkts", ctypes.c_uint32),
            ("dwInUcastPkts", ctypes.c_uint32),
            ("dwInDiscards", ctypes.c_uint32),
            ("dwInErrors", ctypes.c_uint32),
            ("dwInUnknownProtos", ctypes.c_uint32),
            ("dwOutOctets", ctypes.c_uint32),
            ("dwOutNUcastPkts", ctypes.c_uint32),
            ("dwOutUcastPkts", ctypes.c_uint32),
            ("dwOutDiscards", ctypes.c_uint32),
            ("dwOutErrors", ctypes.c_uint32),
            ("dwDescrLen", ctypes.c_uint32),
            ("bDescr", ctypes.c_ubyte * _MAX_INTERFACE_DESCRIPTION_LEN),
        ]

    def _net_octets() -> dict[str, tuple[int, int]]:
        """返回 {网卡描述: (收字节, 发字节)}。网卡名取描述（人类可读），重名时带序号。"""
        buf_size = ctypes.c_ulong(0)
        # 第一次调用拿需要的缓冲区大小（预期返回 ERROR_INSUFFICIENT_BUFFER）
        ctypes.windll.iphlpapi.GetIfTable(None, ctypes.byref(buf_size), 0)
        if buf_size.value == 0:
            return {}
        buf = ctypes.create_string_buffer(buf_size.value)
        if ctypes.windll.iphlpapi.GetIfTable(buf, ctypes.byref(buf_size), 0) != 0:
            raise OSError("GetIfTable 调用失败")
        n_entries = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value
        row_size = ctypes.sizeof(_MIB_IFROW)
        base = ctypes.sizeof(ctypes.c_ulong)  # 表头是一个 ULONG 计数
        result: dict[str, tuple[int, int]] = {}
        for i in range(n_entries):
            start = base + i * row_size
            row = _MIB_IFROW.from_buffer_copy(buf[start: start + row_size])
            name = bytes(row.bDescr[: row.dwDescrLen]).decode("mbcs", errors="replace").rstrip("\x00") or f"if{row.dwIndex}"
            # 同名网卡（常见：WSL/Hyper-V）用索引区分
            key = name if name not in result else f"{name} #{row.dwIndex}"
            result[key] = (row.dwInOctets, row.dwOutOctets)
        return result

    def _uptime_seconds() -> float:
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0

else:
    # Linux / macOS：能读 /proc 就准，读不到就给有限信息
    def _cpu_times() -> tuple[int, int]:
        with open("/proc/stat", "r", encoding="ascii") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts[:8]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return idle, sum(vals)

    def _memory() -> dict:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k.strip()] = int(rest.strip().split()[0]) * 1024  # kB → B
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return {
            "load_pct": round((1 - avail / total) * 100) if total else 0,
            "total": total,
            "avail": avail,
            "page_total": info.get("SwapTotal", 0),
            "page_avail": info.get("SwapFree", 0),
        }

    def _net_octets() -> dict[str, tuple[int, int]]:
        result: dict[str, tuple[int, int]] = {}
        with open("/proc/net/dev", "r", encoding="ascii") as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                fields = rest.split()
                if len(fields) >= 9:
                    result[name.strip()] = (int(fields[0]), int(fields[8]))
        return result

    def _uptime_seconds() -> float:
        with open("/proc/uptime", "r", encoding="ascii") as f:
            return float(f.read().split()[0])


def _gb(n: float) -> str:
    return f"{n / 1024**3:.2f} GB"


def _bar(pct: float, width: int = 20) -> str:
    """文本进度条，让占比一眼可读。"""
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct:.1f}%"


def _cpu_percent(interval: float) -> float:
    idle1, total1 = _cpu_times()
    time.sleep(interval)
    idle2, total2 = _cpu_times()
    d_idle, d_total = idle2 - idle1, total2 - total1
    if d_total <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


@tool(
    "cpu_usage",
    "查看本机 CPU 占用率（百分比）。采样 interval 秒内的平均负载。",
    params={"interval": {"type": "number", "description": "采样窗口秒数（0.2~10），越长越稳", "default": 1.0}},
)
def cpu_usage(interval: float = 1.0) -> str:
    interval = max(0.2, min(10.0, float(interval)))
    try:
        pct = _cpu_percent(interval)
    except (OSError, FileNotFoundError) as e:
        return f"[error] 当前平台无法读取 CPU 占用：{e}"
    return f"CPU 占用率（{interval:.1f}s 采样）: {_bar(pct)}"


@tool(
    "memory_usage",
    "查看本机内存占用：物理内存与页面文件/交换区的总量、已用、可用、百分比。",
)
def memory_usage() -> str:
    try:
        m = _memory()
    except (OSError, FileNotFoundError) as e:
        return f"[error] 当前平台无法读取内存信息：{e}"
    used = m["total"] - m["avail"]
    pct = used / m["total"] * 100 if m["total"] else 0
    page_used = m["page_total"] - m["page_avail"]
    page_pct = page_used / m["page_total"] * 100 if m["page_total"] else 0
    swap_label = "页面文件" if _IS_WIN else "Swap"
    lines = [
        f"物理内存   : {_bar(pct)}",
        f"  总量 {_gb(m['total'])}，已用 {_gb(used)}，可用 {_gb(m['avail'])}",
        f"{swap_label}    : {_bar(page_pct)}" if m["page_total"] else f"{swap_label}: 未启用",
    ]
    if m["page_total"]:
        lines.append(f"  总量 {_gb(m['page_total'])}，已用 {_gb(page_used)}，可用 {_gb(m['page_avail'])}")
    return "\n".join(lines)


@tool(
    "disk_overview",
    "查看本机所有磁盘分区的占用一览（盘符/挂载点、总量、已用、可用、百分比）。",
)
def disk_overview() -> str:
    rows: list[tuple[str, str, str, str, float]] = []
    if _IS_WIN:
        mounts = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    else:
        mounts = ["/"]
    for mnt in mounts:
        try:
            d = shutil.disk_usage(mnt)
        except OSError:
            continue  # 空光驱、弹出中的 U 盘等
        used = d.total - d.free
        pct = used / d.total * 100 if d.total else 0
        rows.append((mnt, _gb(d.total), _gb(used), _gb(d.free), pct))
    if not rows:
        return "[error] 没有找到可读取的磁盘分区"
    lines = [f"{'分区':<6} {'总量':>10} {'已用':>10} {'可用':>10}  占用"]
    for mnt, total, used, free, pct in rows:
        lines.append(f"{mnt:<6} {total:>10} {used:>10} {free:>10}  {_bar(pct, 12)}")
    return "\n".join(lines)


@tool(
    "network_rate",
    "查看本机各网卡的网络收发速率（KB/s）。采样 interval 秒计算平均值。",
    params={"interval": {"type": "number", "description": "采样窗口秒数（0.5~10）", "default": 1.0}},
)
def network_rate(interval: float = 1.0) -> str:
    interval = max(0.5, min(10.0, float(interval)))
    try:
        t1 = _net_octets()
        time.sleep(interval)
        t2 = _net_octets()
    except (OSError, FileNotFoundError) as e:
        return f"[error] 当前平台无法读取网络计数器：{e}"
    lines = [f"网络速率（{interval:.1f}s 采样，单位 KB/s）:"]
    any_traffic = False
    for name, (rx2, tx2) in t2.items():
        rx1, tx1 = t1.get(name, (rx2, tx2))
        rx_rate = max(0, rx2 - rx1) / interval / 1024
        tx_rate = max(0, tx2 - tx1) / interval / 1024
        # 过滤全零网卡，减少噪音（环回/虚拟机网卡通常静默）
        if rx_rate == 0 and tx_rate == 0:
            continue
        any_traffic = True
        lines.append(f"  {name}\n    ↓ 收 {rx_rate:9.1f} KB/s   ↑ 发 {tx_rate:9.1f} KB/s")
    if not any_traffic:
        lines.append("  （采样窗口内所有网卡均无流量）")
    return "\n".join(lines)


@tool(
    "system_uptime",
    "查看本机已开机运行时长。",
)
def system_uptime() -> str:
    try:
        secs = _uptime_seconds()
    except (OSError, FileNotFoundError) as e:
        return f"[error] 当前平台无法读取开机时长：{e}"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    parts.append(f"{minutes} 分钟")
    return f"已开机运行：{' '.join(parts)}（{secs / 3600:.1f} 小时）"


@tool(
    "top_cpu_processes",
    "查看 CPU 占用最高的进程 Top N（需要安装可选依赖 psutil）。",
    params={
        "limit": {"type": "integer", "description": "显示条数", "default": 10},
        "interval": {"type": "number", "description": "采样窗口秒数", "default": 0.5},
    },
)
def top_cpu_processes(limit: int = 10, interval: float = 0.5) -> str:
    try:
        import psutil  # type: ignore
    except ImportError:
        return (
            "[提示] 该工具需要可选依赖 psutil：pip install psutil\n"
            "       装好后即可查看进程级 CPU 占用；内存排序可用 list_processes。"
        )
    interval = max(0.1, min(5.0, float(interval)))
    # 第一遍建立基线，第二遍按 interval 取真实百分比
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(interval)
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_percent"]):
        try:
            cpu = p.cpu_percent(None)
            rows.append((cpu, p.info["pid"], p.info.get("name") or "?", p.info.get("memory_percent") or 0))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    rows.sort(reverse=True)
    lines = [f"{'PID':>7} {'CPU%':>7} {'内存%':>7}  进程名"]
    for cpu, pid, name, mem in rows[: max(1, limit)]:
        lines.append(f"{pid:>7} {cpu:>6.1f} {mem:>6.1f}  {name}")
    return f"CPU Top {min(limit, len(rows))}（采样 {interval:.1f}s）:\n" + "\n".join(lines)


@tool(
    "port_usage",
    "查询本机端口占用：不传 port 列出所有监听端口及占用进程；传入 port 查指定端口被谁占用。",
    params={
        "port": {"type": "integer", "description": "要查询的端口号；不传则列出全部监听端口", "default": 0},
        "protocol": {"type": "string", "description": "tcp / udp / all", "default": "tcp"},
    },
)
def port_usage(port: int = 0, protocol: str = "tcp") -> str:
    # 优先 psutil：一条调用拿到 端口→PID→进程名
    try:
        import psutil  # type: ignore

        kind = {"tcp": "tcp", "udp": "udp", "all": "all"}.get(protocol.lower(), "tcp")
        rows: list[tuple[int, str, int, str]] = []  # (端口, 协议, PID, 进程名)
        for c in psutil.net_connections(kind=kind):
            if not c.laddr:
                continue
            is_listen = c.status == "LISTEN" or c.type == socket.SOCK_DGRAM
            if not is_listen:
                continue
            lport = c.laddr.port
            if port and lport != port:
                continue
            proto = "TCP" if c.type == socket.SOCK_STREAM else "UDP"
            pid = c.pid or 0
            try:
                pname = psutil.Process(pid).name() if pid else "(系统)"
            except Exception:
                pname = "?"
            rows.append((lport, proto, pid, pname))
        return _format_ports(rows, port)
    except ImportError:
        pass
    # 兜底：Windows 解析 netstat（只查 TCP LISTEN）
    if _IS_WIN:
        return _port_usage_netstat(port)
    return "[提示] 该工具需要可选依赖 psutil：pip install psutil"


def _format_ports(rows: list[tuple[int, str, int, str]], port: int) -> str:
    """统一格式化端口清单；rows = [(端口, 协议, PID, 进程名)]。"""
    if not rows:
        if port:
            return f"端口 {port} 当前未被任何进程监听/占用。"
        return "（没有发现监听端口）"
    # 同端口可能因 IPv4/IPv6 双栈各出一行，按 (端口,协议,PID) 去重
    seen: set[tuple[int, str, int]] = set()
    uniq: list[tuple[int, str, int, str]] = []
    for r in rows:
        key = (r[0], r[1], r[2])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    uniq.sort(key=lambda r: (r[0], r[1]))
    lines = [f"{'端口':>7} {'协议':<5} {'PID':>7}  进程"]
    for lport, proto, pid, pname in uniq:
        lines.append(f"{lport:>7} {proto:<5} {pid:>7}  {pname}")
    head = f"端口 {port} 占用情况：" if port else f"监听端口共 {len(uniq)} 条："
    return head + "\n" + "\n".join(lines)


def _port_usage_netstat(port: int = 0) -> str:
    """无 psutil 时的 Windows 兜底：netstat -ano + tasklist 反查进程名。"""
    from core import run_process

    code, out, err, _ = run_process("netstat -ano -p TCP", shell=True, timeout=15)
    if code != 0:
        return f"[error] netstat 执行失败：{err or out}"
    rows: list[tuple[int, str, int, str]] = []
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        # 典型行:  TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING    12345
        if len(parts) < 5 or parts[-2] != "LISTENING":
            continue
        try:
            lport = int(parts[1].rsplit(":", 1)[1])
            pid = int(parts[-1])
        except (ValueError, IndexError):
            continue
        if port and lport != port:
            continue
        rows.append((lport, "TCP", pid, ""))
        pids.add(pid)
    # 批量反查进程名（一次 tasklist，避免每行一次）
    names: dict[int, str] = {}
    if pids:
        code2, out2, _, _ = run_process("tasklist /FO CSV /NH", shell=True, timeout=15)
        if code2 == 0:
            for line in out2.splitlines():
                segs = [s.strip('"') for s in line.split('","')]
                if len(segs) >= 2 and segs[1].isdigit():
                    pid_n = int(segs[1])
                    if pid_n in pids:
                        names[pid_n] = segs[0]
    rows = [(p, proto, pid, names.get(pid, "?")) for p, proto, pid, _ in rows]
    return _format_ports(rows, port)


@tool(
    "machine_status",
    "一页看全本机运行状态：CPU、内存、各磁盘、网络速率、开机时长。适合回答「电脑卡不卡/资源占用如何」。",
    params={"interval": {"type": "number", "description": "CPU/网络的采样窗口秒数", "default": 1.0}},
)
def machine_status(interval: float = 1.0) -> str:
    interval = max(0.5, min(10.0, float(interval)))
    # CPU 与网络共用同一段采样窗口，避免两次等待
    sections: list[str] = []
    try:
        cpu_idle1, cpu_total1 = _cpu_times()
        net1 = _net_octets()
        time.sleep(interval)
        cpu_idle2, cpu_total2 = _cpu_times()
        net2 = _net_octets()
        d_idle, d_total = cpu_idle2 - cpu_idle1, cpu_total2 - cpu_total1
        cpu_pct = max(0.0, min(100.0, (1 - d_idle / d_total) * 100)) if d_total > 0 else 0.0
        sections.append(f"CPU    : {_bar(cpu_pct)}（{os.cpu_count() or '?'} 核）")
        net_lines = []
        for name, (rx2, tx2) in net2.items():
            rx1, tx1 = net1.get(name, (rx2, tx2))
            rx_rate = max(0, rx2 - rx1) / interval / 1024
            tx_rate = max(0, tx2 - tx1) / interval / 1024
            if rx_rate == 0 and tx_rate == 0:
                continue
            net_lines.append(f"  {name}: ↓{rx_rate:.1f} ↑{tx_rate:.1f} KB/s")
        sections.append("网络   :\n" + ("\n".join(net_lines) if net_lines else "  （无流量）"))
    except (OSError, FileNotFoundError) as e:
        sections.append(f"CPU/网络: [当前平台不可读: {e}]")
    try:
        m = _memory()
        used = m["total"] - m["avail"]
        pct = used / m["total"] * 100 if m["total"] else 0
        sections.append(f"内存   : {_bar(pct)}（已用 {_gb(used)} / {_gb(m['total'])}）")
    except (OSError, FileNotFoundError) as e:
        sections.append(f"内存   : [当前平台不可读: {e}]")
    # 磁盘复用 disk_overview 的核心逻辑
    disks: list[str] = []
    mounts = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")] if _IS_WIN else ["/"]
    for mnt in mounts:
        try:
            d = shutil.disk_usage(mnt)
        except OSError:
            continue
        pct = (d.total - d.free) / d.total * 100 if d.total else 0
        disks.append(f"  {mnt:<5} {_bar(pct, 12)} 可用 {_gb(d.free)}")
    sections.append("磁盘   :\n" + ("\n".join(disks) if disks else "  （无）"))
    try:
        up = _uptime_seconds()
        sections.append(f"开机   : {up / 3600:.1f} 小时")
    except (OSError, FileNotFoundError):
        pass
    header = f"本机状态总览（采样 {interval:.1f}s，{time.strftime('%H:%M:%S')}）"
    return header + "\n" + "\n".join(sections)
