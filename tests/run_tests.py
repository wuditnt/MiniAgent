"""本地测试统一入口 —— 一条命令跑完，不用记八个文件名。

用法（先激活虚拟环境，或直接用 .venv 里的解释器）：

    python tests/run_tests.py            # 跑全部免费用例（不需要 API Key）
    python tests/run_tests.py loop       # 只跑名字含 loop / plugins 的套件
    python tests/run_tests.py --list     # 列出所有套件
    python tests/run_tests.py --real     # 额外跑真实 API 用例（会消耗 token）
    python tests/run_tests.py --fail-fast  # 第一个失败就停
    python tests/run_tests.py --timeout 600  # 单套件超时（默认 300s）

为什么需要它？
    · 八个文件各自 `python test_x.py` 也能跑，但「全绿」需要依次执行八次，
      而且失败之后要自己往回翻屏找是哪个套件出问题。这里统一收集退出码、
      记录耗时、打印一张汇总表，失败时**完整回显该套件的输出**。
    · 顺带解决两个真实的坑：

      坑一 —— 解释器必须和「发起 MCP 子进程的那个」一致，
      否则 `mcp/mcp_demo_server.py` 会在一个没装依赖的解释器里启动。
      本脚本在对子进程的调用里显式使用 sys.executable，并把
      MINI_AGENT_PYTHON 传给测试进程 —— 见 _env()。

      坑二 —— 测试文件住在 tests/，但它们引用的 demo_case/、插件目录、
      mcp_servers.json 都在**项目根**。所以子进程的 cwd 必须是 ROOT，
      不是本文件所在目录。这两个概念在这里被刻意分成两个变量，
      混用会让「路径越界」的报错看起来毫无头绪。

退出码：0 = 全绿；1 = 有用例失败；2 = 用法错误 / 环境不满足。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

#: 本文件所在目录 —— `tests/`
HERE = os.path.dirname(os.path.abspath(__file__))
#: 项目根目录 —— 沙箱根、插件目录、mcp_servers.json 的基准。测试进程的 cwd。
ROOT = os.path.dirname(HERE)
#: 源码布局根 —— `src/`，要放上 sys.path 才能 `import core`
SRC = os.path.join(ROOT, "src")
#: 包目录 —— `src/core/`，裸模块名（import tools / from core import / import agent）的落点
PKG = os.path.join(SRC, "core")


# ---------------------------------------------------------------- 套件清单

# (文件名, 说明, 是否需要 API Key) —— 路径相对于本文件所在的 tests/
#
# 免费用例 22 个 + 真实用例 1 个。真实用例默认跳过（加 --real 才跑，会消耗 token）。
# 关于真实用例只有一个：它的价值是「端到端验真」—— 真模型 + 真 MCP 子进程，
# 一次性覆盖「本端立刻停 + 通知真的发出去 + 服务端真的收手」这条完整链路，
# 比拆成两个半覆盖的用例更省 token，也不会有「两个都跑一半」的重复开销。
SUITES: list[tuple[str, str, bool]] = [
    ("test_loop.py", "循环层 6 项：假客户端，不碰网络", False),
    ("test_plugins.py", "插件层 + MCP 层 12 项", False),
    ("test_skills.py", "技能层 9 项：frontmatter / 加载 / 冲突 / 热重载 / use_skill / 来源分类 / 消息解析 / 本轮注入", False),
    ("test_vision.py", "图片输入 5 项：嗅探 / 附件通道 / 拒绝 / 循环集成", False),
    ("test_stream_parallel.py", "流式聚合 + 并行执行 + MCP 并发 19 项", False),
    ("test_cancel.py", "取消机制 11 项：令牌 / 杀进程 / 组隔离 / 流中断 / 消息补齐 / 原因送达", False),
    ("test_mcp_cancel.py", "MCP 取消通知 8 项：送达 / 停手 / 迟到响应 / 幂等 / 定向", False),
    ("test_http_e2e.py", "本地 mock HTTP（支持 SSE）：流式三条降级路径 + 并行收益", False),
    ("test_robustness.py", "健壮性回归 4 项：迁移不覆盖 / NaN 配置 / 工作区路径校验 / MCP 非对象 JSON", False),
    ("test_webui.py", "Web UI 端到端 22 项：SSE 事件序列 / 双模式工具表 / 轨迹回放 / 停止 / 确认 / 409 / 用户指令记忆 / 重启后会话模型恢复 / keep-alive / 接口兜底与限权", False),
    ("test_sys_monitor.py", "系统监控插件 8 项：CPU / 内存 / 磁盘 / 网络 / 开机时长 / 端口占用", False),
    ("test_memory.py", "记忆系统 15 项：存储 / 保护区 / 工具不可见 / prelude / shell / 注入 / 分级淘汰 / 提炼过滤 / 检索合并 / 显式指令", False),
    ("test_logging.py", "文件日志 6 项：按日目录 / 错误聚合 / 幂等重配 / 关闭 / 配置 / 清理", False),
    ("test_workbench.py", "工作台文件接口 9 项：目录 / 安全 / 编码 / 图片 / HTML / MHTML", False),
    ("test_events.py", "事件流 10 项：只追加 / 请求可重建 / 正文不截断 / 裁剪留痕 / 开关 / 兜底 / 非法 id", False),
    ("test_model_config.py", "模型配置 10 项：厂家 spec（low/high/max）/ 三种参数风格与 values 重映射 / 迁移 / 会话解析 / 地址协议校验 / 密钥文件权限", False),
    ("test_web_search.py", "联网搜索 3 项：后端选择 / DeepSeek Key 复用 / 请求与解析", False),
    ("test_auth.py", "登录与账号 10 项：建库 / 默认超管 / 哈希 / 会话 / 限流 / 保护规则 / 已禁用超管降级 / 改密 / 写入节流", False),
    ("test_net_tools.py", "网络工具 4 项：云端/本地分派 / 失败回退 / 协议校验 / Fetch 请求解析", False),
    ("test_ask.py", "内联提问 16 项：返回协议 / 选项规范化 / 工具契约 / 等待桥 / 关闭 UI 不阻塞", False),
    ("test_approval.py", "审批默认值 4 项：无通道默认拒绝 / 显式 opt-in / 批准拒绝 / 桥故障", False),
    ("test_concurrency.py", "并发与全局状态 4 项：询问通道隔离 / copy_context 传播 / 注册表切换 / 配置并发写", False),
    ("test_real_mcp_cancel.py", "真实模型 + 真 MCP server：取消通知全链路", True),
]


def _env() -> dict[str, str]:
    """给测试进程准备环境。

    三件事必须做对，否则测试会莫名其妙地失败：

    1. MINI_AGENT_WORKSPACE 指向项目根 —— 沙箱根目录。不设的话它会取
       os.getcwd()，从别的目录调用 run_tests.py 就会「路径越界」。
    2. sys.executable 必须是最外层解释器，所以要让它一路传到底：
       测试进程用 sys.executable 拉起 mcp 子进程；
       MCP 客户端又只在 command 恰好等于 python/python3 时才改写它。
       这里额外导出 MINI_AGENT_PYTHON，给需要显式指定的场景用。
    3. MINI_AGENT_ROOT 与 PYTHONPATH —— 让测试进程找到 core 包。
       我们把 ROOT/src 与 ROOT/src/core 都塞进 PYTHONPATH：
       前者支持 `import core`，后者支持 `import agent` /
       `from core import ...` / `import tools` 这些裸模块名写法。
    """
    env = os.environ.copy()
    env["MINI_AGENT_WORKSPACE"] = ROOT
    env["MINI_AGENT_ROOT"] = ROOT
    env["MINI_AGENT_PYTHON"] = sys.executable
    # 子进程输出别用花哨编码，Windows 控制台容易看不懂
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")

    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    for p in (PKG, SRC, ROOT):
        if p not in existing:
            existing.insert(0, p)
    env["PYTHONPATH"] = os.pathsep.join(existing)
    return env


# ---------------------------------------------------------------- 执行

def run_one(path: str, desc: str, timeout: float) -> tuple[bool, float, str]:
    """跑一个套件，返回 (是否通过, 耗时, 输出)。

    path 是相对 tests/ 的文件名；cwd 是 ROOT —— 两者刻意不同，
    见模块文档里的「坑二」。
    """
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, path)],
            cwd=ROOT,
            env=_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, time.time() - t0, out
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode("utf-8", "replace")
               if isinstance(e.stdout, bytes) else (e.stdout or ""))
        return False, time.time() - t0, f"[超时 {timeout:.0f}s] 已终止\n{out}"
    except FileNotFoundError as e:
        return False, 0.0, f"[无法运行] {e}"


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="run_tests",
        description="mini-agent 本地测试统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("filters", nargs="*", help="只跑名字里含这些关键词的套件")
    ap.add_argument("--list", action="store_true", help="列出所有套件后退出")
    ap.add_argument("--real", action="store_true", help="额外包含真实 API 用例（消耗 token）")
    ap.add_argument("--fail-fast", action="store_true", help="第一个失败就停")
    ap.add_argument("--timeout", type=float, default=300.0, help="单套件超时秒数（默认 300）")
    args = ap.parse_args()

    if args.list:
        for name, desc, needs_key in SUITES:
            flag = " [需 API Key]" if needs_key else ""
            print(f"  {name:<26} {desc}{flag}")
        return 0

    if args.filters:
        kws = [k.lower() for k in args.filters]

        def keep(name: str) -> bool:
            return any(k in name.lower() for k in kws)
    else:
        def keep(name: str) -> bool:
            return True

    selected = [
        (n, d, need) for n, d, need in SUITES
        if keep(n) and (not need or args.real)
    ]
    if not selected:
        print("没有匹配的套件。用 --list 看看有哪些。")
        return 2

    try:
        import core.model_config as _mc

        has_key = bool(_mc.resolve_active().get("api_key"))
    except Exception:
        has_key = False
    print(f"解释器：{sys.executable}")
    print(f"项目根：{ROOT}")
    print(f"工作区：{os.environ.get('MINI_AGENT_WORKSPACE') or os.environ.get('AGENT_WORKSPACE', ROOT)}")
    print(f"套件数：{len(selected)}"
          f"{'' if args.real else '（免费用例；加 --real 可包含真实 API 用例）'}")
    skipped = [n for n, _d, need in SUITES if need and not args.real]
    if skipped:
        print(f"已跳过：{', '.join(skipped)}")
    print()

    results: list[tuple[str, bool, float, str]] = []
    for idx, (name, desc, _need) in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] {name}  —— {desc}", flush=True)
        ok, secs, out = run_one(name, desc, args.timeout)
        mark = "PASS" if ok else "FAIL"
        print(f"          → {mark}（{secs:.1f}s）", flush=True)
        if not ok:
            print("\n" + "-" * 68)
            print(f"{name} 输出：")
            print(out.rstrip())
            print("-" * 68 + "\n")
            if args.fail_fast:
                results.append((name, ok, secs, out))
                break
        results.append((name, ok, secs, out))

    # ---------------------------------------------------------- 汇总
    passed = sum(1 for _n, ok, _s, _o in results if ok)
    total = len(results)
    wall = sum(secs for _n, _ok, secs, _o in results)
    print("\n" + "=" * 68)
    print("汇总")
    print("=" * 68)
    for name, ok, secs, _out in results:
        print(f"  {'✓' if ok else '✗'} {name:<26} {secs:6.2f}s")
    print(f"\n  {passed}/{total} 通过 | 累计耗时 {wall:.1f}s")

    if passed != total:
        print("\n失败详情见上方各套件的完整输出。")
        if not has_key:
            print("提示：真实 API 用例需要先在 Web UI 配置模型（models.json）。")
        return 1
    print("\n全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
