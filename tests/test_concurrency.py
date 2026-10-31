"""
并发与全局状态测试 —— 免费用例：纯内存 + 临时配置文件，不碰网络、不烧 token。

覆盖三处「多会话并发时会被串味」的全局状态：
  · ask / approval 的询问通道：必须是 ContextVar，不能是模块级变量 ——
    否则 A 会话的提问会弹到 B 会话上，A 结束时还会把仍在跑的 B 的通道清空；
  · 工具注册表切换：工具导入期间的 use_registry 不能影响并发的另一个导入；
  · models.json 写入：并发「读-改-写」必须串行，否则后写的覆盖先写的。

另外验证「工作线程拿不到主线程的 ContextVar」这件事实：agent 并行执行工具时
必须用 contextvars.copy_context() 提交，否则工具里读不到询问通道（→ 审批被拒）。
"""

from __future__ import annotations

import contextvars
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import approval, ask, registry  # noqa: E402
from core.registry import ToolRegistry  # noqa: E402

import core.model_config as mc  # noqa: E402


# ---------------------------------------------------------------- 询问通道隔离

def test_asker_isolated_between_threads() -> None:
    """两个线程各自注入 asker：互不覆盖，也互不清空。"""
    results: dict[str, tuple[bool, str]] = {}
    barrier = threading.Barrier(2)

    def worker(tag: str, approve: bool) -> None:
        tok_a = approval.set_asker(lambda _r: approve)
        tok_q = ask.set_asker(lambda *_a: tag)
        try:
            barrier.wait()   # 两个线程都注入完再读，确保「串味」会被暴露
            results[tag] = (
                approval.request_approval("x").startswith("[approved]"),
                ask.ask_user("选哪个？", ["A", "B"]),
            )
        finally:
            approval.reset_asker(tok_a)
            ask.reset_asker(tok_q)

    t1 = threading.Thread(target=worker, args=("A", True))
    t2 = threading.Thread(target=worker, args=("B", False))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["A"] == (True, "[answer] A"), results
    assert results["B"] == (False, "[answer] B"), results
    # 线程结束后主上下文仍然是「无通道」，没有被线程的注入污染
    assert approval.has_asker() is False
    assert ask.has_asker() is False


def test_copy_context_propagates_into_worker() -> None:
    """工作线程默认拿不到主线程的 ContextVar —— 这正是 agent 必须 copy_context 的原因。"""
    tok_a = approval.set_asker(lambda _r: True)
    tok_q = ask.set_asker(lambda *_a: "ok")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        absent = pool.submit(lambda: (approval.has_asker(), ask.has_asker())).result()
        # 与 agent._run_tools 一致：每个任务各复制一份 Context 提交
        present = pool.submit(
            contextvars.copy_context().run, lambda: (approval.has_asker(), ask.has_asker())
        ).result()
    finally:
        pool.shutdown()
        approval.reset_asker(tok_a)
        ask.reset_asker(tok_q)

    assert absent == (False, False), f"未复制上下文时工作线程不该看到通道：{absent}"
    assert present == (True, True), f"复制上下文后工作线程应看到通道：{present}"


# ---------------------------------------------------------------- 注册表切换

def test_use_registry_isolated_between_threads() -> None:
    """两个线程各自 use_registry：@tool 必须落进各自的注册表，且不污染全局。"""
    regs = {"a": ToolRegistry(), "b": ToolRegistry()}
    seen: dict[str, bool] = {}
    barrier = threading.Barrier(2)

    def worker(key: str) -> None:
        reg = regs[key]
        with registry.use_registry(reg):
            @registry.tool(f"t_{key}", f"tool {key}")
            def _fn() -> str:
                return key

            barrier.wait()
            seen[key] = registry.active_registry() is reg

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert seen == {"a": True, "b": True}, seen
    assert [t.name for t in regs["a"].all()] == ["t_a"]
    assert [t.name for t in regs["b"].all()] == ["t_b"]
    assert registry.REGISTRY.get("t_a") is None, "全局注册表不该收到临时注册表的工具"
    assert registry.REGISTRY.get("t_b") is None
    assert registry.active_registry() is registry.REGISTRY


# ---------------------------------------------------------------- 配置并发写

def test_model_config_concurrent_writes() -> None:
    """20 个线程同时新增配置：一个都不能丢，文件必须始终是合法 JSON。"""
    tmpdir = tempfile.mkdtemp(prefix="miniagent-mc-conc-")
    mc._MODELS_FILE = os.path.join(tmpdir, "models.json")
    # 基线：首次读会按环境变量造一条默认记录，它也算「必须保留」的那条
    base = {w["name"] for w in mc._load_raw()["providers"]}

    def add(i: int) -> str:
        return mc.create_entry(
            "ollama", f"m{i}", "http://127.0.0.1:11434/v1", "", f"qwen{i}"
        )["name"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(add, range(20)))

    with open(mc._MODELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)     # 半截文件会在这里直接抛 JSONDecodeError

    saved = {w.get("name") for w in data.get("providers", [])}
    expected = base | set(names)
    assert expected <= saved, f"并发写丢了配置：{expected - saved}"
    assert len(saved) == len(expected), f"应恰好 {len(expected)} 条，实际 {len(saved)}"
    # 临时文件必须清理干净（唯一 mkstemp 文件 + 异常分支 unlink）
    assert not [n for n in os.listdir(tmpdir) if n.endswith(".tmp")], os.listdir(tmpdir)


if __name__ == "__main__":
    test_asker_isolated_between_threads()
    test_copy_context_propagates_into_worker()
    test_use_registry_isolated_between_threads()
    test_model_config_concurrent_writes()
    print("✓ 并发与全局状态 4 项通过")
