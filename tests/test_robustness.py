"""
批次 4a 回归：只覆盖「不改变核心语义、但错得很难受」的健壮性修复。

  1. 数据目录迁移只补缺失文件，绝不覆盖已有文件（否则不可逆丢数据）
  2. 配置拒绝 NaN / inf（否则能绕过区间校验 / 让 int() 抛 OverflowError）
  3. 工作区路径不能为空（空串会被 abspath 变成 cwd）；默认工作区路径不可改
  4. MCP 收到非对象 JSON（[] / 123 / null）时不能打死读线程
  5. 工作区根 / 联网模式按上下文隔离（并发会话不再互相覆盖）
"""

from __future__ import annotations

import os
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import settings  # noqa: E402
from core._paths import _copy_missing  # noqa: E402
from core.mcp import MCPStdioClient  # noqa: E402

import core.workspace_config as wc  # noqa: E402


def test_copy_missing_never_overwrites() -> None:
    src = tempfile.mkdtemp(prefix="miniagent-mig-src-")
    dst = tempfile.mkdtemp(prefix="miniagent-mig-dst-")
    os.makedirs(os.path.join(src, "sessions"))
    with open(os.path.join(src, "settings.json"), "w", encoding="utf-8") as f:
        f.write("OLD")
    with open(os.path.join(src, "sessions", "s1.json"), "w", encoding="utf-8") as f:
        f.write("OLD-SESSION")
    # 目标目录里已经有「新」数据：迁移必须保留它
    with open(os.path.join(dst, "settings.json"), "w", encoding="utf-8") as f:
        f.write("NEW")

    _copy_missing(src, dst)

    with open(os.path.join(dst, "settings.json"), encoding="utf-8") as f:
        assert f.read() == "NEW", "迁移覆盖了目标目录里已有的设置"
    with open(os.path.join(dst, "sessions", "s1.json"), encoding="utf-8") as f:
        assert f.read() == "OLD-SESSION", "缺失的文件应被补齐"
    print("✓ 迁移：已有文件不覆盖，缺失文件补齐")


def test_settings_reject_non_finite() -> None:
    for key, bad in (("temperature", "nan"), ("max_turns", "inf"), ("max_turns", "-inf")):
        try:
            settings.coerce(key, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key}={bad} 应该被拒绝")
    assert settings.coerce("temperature", "1.5") == 1.5
    assert settings.coerce("max_turns", "7") == 7
    print("✓ 配置：NaN / ±inf 被拒绝，正常数字不受影响")


def test_workspace_rejects_empty_and_default_path_change() -> None:
    wc._WORKSPACES_FILE = os.path.join(
        tempfile.mkdtemp(prefix="miniagent-ws-"), "workspaces.json"
    )
    try:
        wc.create_workspace("空路径", "")
    except ValueError as e:
        assert "不能为空" in str(e), str(e)
    else:
        raise AssertionError("空路径应被拒绝（不能把 cwd 当工作区）")

    other = tempfile.mkdtemp(prefix="miniagent-ws-other-")
    try:
        wc.update_workspace(wc.DEFAULT_ID, path=other)
    except ValueError as e:
        assert "默认工作区" in str(e), str(e)
    else:
        raise AssertionError("默认工作区的路径不应被静默接受")

    # 前端编辑弹窗会把原路径一起提交：与当前一致时应只处理改名
    item = wc.update_workspace(wc.DEFAULT_ID, name="主工作区", path=wc._default_path())
    assert item["name"] == "主工作区"
    assert item["path"] == wc._default_path()
    print("✓ 工作区：空路径被拒；默认工作区改路径被拒、同名路径只改名")


def test_mcp_dispatch_ignores_non_object_json() -> None:
    client = MCPStdioClient(name="probe", command="echo")
    client._dispatch([])        # 旧实现：AttributeError 直接打死读线程
    client._dispatch(123)
    client._dispatch(None)
    assert len(client._stdout_noise) == 3, client._stdout_noise
    assert client._rx.empty(), "非对象消息不该被当成 server 主动通知入队"
    # 正常对象仍然照常投递
    client._dispatch({"jsonrpc": "2.0", "method": "notifications/progress"})
    assert not client._rx.empty()
    print("✓ MCP：非对象 JSON 归为噪声，读线程与公共队列不受影响")


def test_workspace_and_net_mode_isolated_per_context() -> None:
    """并发会话各用各的沙箱根 / 联网模式（模块全局 → ContextVar）。"""
    import contextvars
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from core import net_mode, sandbox

    base = sandbox.workspace()
    root_a = tempfile.mkdtemp(prefix="miniagent-ctx-a-")
    root_b = tempfile.mkdtemp(prefix="miniagent-ctx-b-")
    seen: dict[str, tuple[str, str]] = {}

    def worker(name: str, path: str, mode: str) -> None:
        sandbox.set_workspace(path)
        net_mode.set_mode(mode)
        time.sleep(0.05)          # 留出交错窗口：旧实现下后设的会覆盖先设的
        seen[name] = (sandbox.workspace(), net_mode.get_mode())

    threads = [
        threading.Thread(target=worker, args=("a", root_a, "cloud")),
        threading.Thread(target=worker, args=("b", root_b, "local")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen["a"] == (os.path.abspath(root_a), "cloud"), seen
    assert seen["b"] == (os.path.abspath(root_b), "local"), seen
    # 子线程的设置不能污染调用方上下文
    assert sandbox.workspace() == base
    assert net_mode.get_mode() == "local"

    # 并行工具线程靠 copy_context 传播：这里验证该机制对这两个 ContextVar 有效
    net_mode.set_mode("cloud")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            got = pool.submit(
                contextvars.copy_context().run, lambda: (sandbox.workspace(), net_mode.get_mode())
            ).result(timeout=5)
        assert got == (base, "cloud"), got
    finally:
        net_mode.reset()
    print("✓ 上下文隔离：工作区根与联网模式互不串线，copy_context 可传播")


if __name__ == "__main__":
    test_copy_missing_never_overwrites()
    test_settings_reject_non_finite()
    test_workspace_rejects_empty_and_default_path_change()
    test_mcp_dispatch_ignores_non_object_json()
    test_workspace_and_net_mode_isolated_per_context()
    print("---")
    print("全部通过 ✅")
