"""
系统监控插件测试：验证 sys_monitor.py 里的工具在当前平台真实返回数据。

不花 API Key，不调外部服务，只读本机计数器。
"""

from __future__ import annotations

import os
import re

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core.registry import REGISTRY

_done = False


def _ensure_loaded() -> None:
    """把内置插件目录加载进全局注册表（只做一次，避免同名工具进 skipped）。"""
    global _done
    if _done:
        return
    from loader import PluginLoader
    from bootstrap import PACKAGE_ROOT as HERE

    ldr = PluginLoader(HERE, dirs=["plugins"], registry=REGISTRY)
    ldr.load_all()
    _done = True


def _call(name: str, **args) -> str:
    _ensure_loaded()
    return REGISTRY.call(name, args)


def test_cpu_usage():
    out = _call("cpu_usage", interval=0.2)
    assert "CPU 占用率" in out, out
    # 匹配 0.0%~100.0%
    m = re.search(r"(\d+\.\d)%", out)
    assert m is not None, out
    assert 0.0 <= float(m.group(1)) <= 100.0
    print(f"✓ CPU：{m.group(1)}%")


def test_memory_usage():
    out = _call("memory_usage")
    assert "物理内存" in out, out
    assert "GB" in out, out
    print(f"✓ 内存：返回成功（{out.splitlines()[0]}）")


def test_disk_overview():
    out = _call("disk_overview")
    assert "分区" in out and "GB" in out, out
    # Windows 至少能看到 C:\
    assert re.search(r"[A-Z]:\\", out), out
    print(f"✓ 磁盘：返回成功（{len(out.splitlines()) - 1} 个分区）")


def test_network_rate():
    out = _call("network_rate", interval=0.5)
    assert "网络速率" in out, out
    assert "KB/s" in out or "无流量" in out, out
    print("✓ 网络：返回成功")


def test_system_uptime():
    out = _call("system_uptime")
    assert "已开机运行" in out or "[error]" in out, out
    print(f"✓ 开机：{out.splitlines()[0]}")


def test_top_cpu_processes():
    out = _call("top_cpu_processes", limit=3)
    # psutil 装了就是表格，没装就是提示
    assert "CPU" in out and ("PID" in out or "psutil" in out), out
    print(f"✓ CPU Top：返回成功（{'psutil 增强' if 'PID' in out else '可选依赖提示'}）")


def test_machine_status():
    out = _call("machine_status", interval=0.5)
    assert "本机状态总览" in out, out
    assert "CPU" in out and "内存" in out and "磁盘" in out, out
    print("✓ 聚合：一页总览返回成功")


def test_port_usage():
    import socket

    # 自己起一个临时监听端口，验证「指定端口 → 找到本进程」链路
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    lport = srv.getsockname()[1]
    try:
        out = _call("port_usage", port=lport)
        assert str(lport) in out, out
        assert str(os.getpid()) in out, f"应显示本进程 PID：{out}"
        # 空闲端口应明确报告未占用
        srv2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv2.bind(("127.0.0.1", 0))
        free_port = srv2.getsockname()[1]
        srv2.close()
        out2 = _call("port_usage", port=free_port)
        assert "未被" in out2, out2
        # 全量列表
        out3 = _call("port_usage")
        assert "监听端口" in out3, out3
        print(f"✓ 端口占用：指定端口命中 PID，空闲端口正确报告，全量列表正常")
    finally:
        srv.close()


if __name__ == "__main__":
    test_cpu_usage()
    test_memory_usage()
    test_disk_overview()
    test_network_rate()
    test_system_uptime()
    test_top_cpu_processes()
    test_machine_status()
    test_port_usage()
    print("---")
    print("全部通过 ✅")
