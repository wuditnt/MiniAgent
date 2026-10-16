"""
真实 API + 真实 MCP server 的取消通知验证（会消耗少量 token）。

这是项目里**唯一**消耗 token 的用例，因为它是唯一能一次验完整条链路的：

    「本端能不能立刻停」+「通知真的发出去了」+「对端真的收手了」

链路是完整生产形态：

    真实模型（DeepSeek，流式）
        → 决定调用 MCP 工具 demo__sleep(ms=8000)
        → mini-agent 通过 stdio 把 tools/call 发给子进程 mcp/mcp_demo_server.py
        → 工具跑到一半，用户取消（等价 Ctrl+C 的内部路径）
        → 客户端立刻收手，并补发 notifications/cancelled{requestId, reason}
        → 服务端收到通知 → 提前收手 → 按规范**不回响应**

「本端立刻停」这条本身也有免费覆盖（tests/test_cancel.py 用真子进程验杀进程树），
所以这里不重复测它，只把它当作链路的前半段一并确认。

预期：
  1 run() 在 ~2s 内抛 Cancelled（工具本来要跑 8s）；
  2 客户端的取消通知计数 +1，reason 里带着本地取消原因；
  3 服务端的 cancel_log 里能看到同一个 requestId 与 reason；
  4 服务端不回复该请求（客户端一条迟到响应都没收到）；
  5 消息序列保持合法，取消后还能继续对话。

用 UI 观察者把取消锚定在「MCP 工具已经开始执行」之后，比固定延时确定。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

from main import load_dotenv

load_dotenv()

from agent import Agent                                   # noqa: E402
from core.cancel import Cancelled                          # noqa: E402
from core.mcp import MCPManager                            # noqa: E402
from core.registry import REGISTRY                         # noqa: E402
from ui import make_ui                                     # noqa: E402

CANCEL_REASON = "真实场景：用户不耐烦，按下了 Ctrl+C"


def wait_for(probe, timeout: float = 3.0, interval: float = 0.05):
    """轮询等待 —— 通知的**处理**发生在服务端的另一个线程里，
    「收到通知」与「工具停手后的日志」之间天然有几十毫秒的间隔。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe():
            return True
        time.sleep(interval)
    return False


class CancelOnMcpToolUI:
    """包装真实 UI：第一次看到 MCP 工具开始执行时，延迟触发取消。"""

    def __init__(self, inner, agent, delay: float = 1.2):
        self._inner = inner
        self._agent = agent
        self._delay = delay
        self._fired = False
        self.notice_shown = None

    def tool_start(self, turn, tool, args):
        if not self._fired and tool.source == "mcp":
            self._fired = True
            name = tool.name

            def _cancel():
                print(f"\n>>> {name} 已执行 {self._delay}s，触发取消（等价 Ctrl+C）<<<")
                self._agent.interrupt(CANCEL_REASON)

            threading.Timer(self._delay, _cancel).start()
        return self._inner.tool_start(turn, tool, args)

    def cancelled(self, reason: str = "") -> bool:
        self.notice_shown = reason
        print(f"\n[UI 取消通知] {reason}")
        return self._inner.cancelled(reason)

    def __getattr__(self, name):
        # 其余事件（llm_delta / tools_start / ask_confirm…）透传给真实 UI。
        # 注意：__getattr__ 只在常规查找失败时才被调用，
        # 所以上面显式实现的 tool_start / cancelled 不会被它抢走。
        return getattr(self._inner, name)


def main() -> int:
    assert os.environ.get("OPENAI_API_KEY"), "缺少 OPENAI_API_KEY（.env）"

    mcp = MCPManager(os.path.join(HERE, "mcp", "mcp_servers.json"), HERE, REGISTRY)
    mcp.load()
    print(f"MCP: {mcp.report.summary()}")
    assert "demo__sleep" in REGISTRY, "demo MCP server 未挂载"

    agent = Agent(verbose=True)
    ui = CancelOnMcpToolUI(agent.ui, agent)
    agent.ui = ui

    checks: list[tuple[str, bool, str]] = []

    print("\n=== 第 1 步：下达任务（模型会调用 MCP 的 sleep 工具睡 8 秒）===\n")
    t0 = time.time()
    try:
        agent.run(
            "请调用 MCP 工具 demo__sleep，参数 ms=8000、tag=\"real-cancel\"。"
            "等它返回后把返回内容原样告诉我。不要用其它工具，不要用命令行替代它。"
        )
        print("\n[异常] 任务居然正常完成了 —— 取消没有生效")
        return 1
    except Cancelled as e:
        elapsed = time.time() - t0
        checks.append(("run() 抛出 Cancelled（工具本要跑 8s）", elapsed < 10, f"{elapsed:.2f}s"))
        checks.append(("Cancelled 文案带本地原因", CANCEL_REASON[:12] in str(e), str(e)))

    checks.append(("UI 收到取消提示（含原因）", bool(ui.notice_shown), str(ui.notice_shown or "")))
    checks.append(("agent 记录了提示已显示", bool(agent.last_cancel_notice_shown), ""))

    # ---------------------------------------------------------- 通知是否真的发出/送达
    cs = mcp.cancel_stats()
    checks.append(("客户端已补发 1 条取消通知", cs["cancellations"] == 1, json.dumps(cs)))
    checks.append(("没有丢弃不到的迟到响应", cs["late_responses"] == 0, json.dumps(cs)))

    client = mcp.clients["demo"]
    received = []
    deadline = time.time() + 3.0
    while time.time() < deadline:
        data = json.loads(client.call_tool("cancel_log", {}))
        if data["received"]:
            received = data["received"]
            break
        time.sleep(0.05)
    checks.append(("服务端收到通知且 reason 已转述", bool(received) and "Ctrl+C" in received[0]["reason"],
                   json.dumps(received, ensure_ascii=False)))
    # 服务端停手是异步发生的（通知先到、工具线程下一个检查点才退出），要等一下
    stopped = wait_for(lambda: any("提前收手且不回响应" in l for l in client.stderr_tail(50)))
    server_log = " / ".join(client.stderr_tail(4))
    checks.append(("服务端提前收手且不回响应", stopped, server_log[-160:]))

    # ---------------------------------------------------------- 消息序列与后续对话
    last = next(m for m in reversed(agent.messages)
                if m.get("role") == "assistant" and m.get("tool_calls"))
    replies = {m["tool_call_id"]: m["content"] for m in agent.messages if m.get("role") == "tool"}
    checks.append((f"{len(last['tool_calls'])} 个 tool_call 全部有回复",
                   all(tc["id"] in replies for tc in last["tool_calls"]), ""))
    checks.append(("回复里明确写了被取消",
                   any("[cancelled]" in c for c in replies.values()), ""))

    print("\n=== 第 2 步：取消后立即继续对话（消息序列必须合法）===")
    answer = agent.run("刚才被取消了。请只用一句话确认，不要再调用任何工具。")
    if not getattr(agent, "last_answer_streamed", False):
        print()
    checks.append(("取消后的新对话正常完成", bool(answer.strip()), answer.strip()[:60]))

    u = agent.usage
    print(f"\n[统计] LLM {u['llm_calls']} 次 | 工具 {u['tool_calls']} 次 | tokens {u['total_tokens']}")

    print("\n--- 验证点 ---")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        tail = f"  —  {detail}" if detail else ""
        print(f"  {'✓' if passed else '✗'} {name}{tail}")
    print("\n结果:", "全部通过 ✓" if ok else "存在问题 ✗")

    mcp.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
