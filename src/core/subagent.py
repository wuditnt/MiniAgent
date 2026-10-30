"""
子智能体：把「中间过程长、结论短」的子任务放到独立上下文里跑。

为什么需要它：主对话里最贵的往往不是问题本身，而是一串工具调用堆出来的过程
（读十几个文件、翻几十页 PDF）。这些中间结果留在主上下文里，每一轮都要重新付费。
子代理在自己的 messages 里跑完，只把结论交回来，过程随对象一起丢弃。

契约（由 tests/test_subagent.py 守住，改动前先看那些用例）：
  · 不带主对话历史 —— 子代理只看得到「任务包」，所以任务包必须自包含；
  · 只读工具子集 —— 没有写文件 / 执行命令 / 提问 / 再派子代理；
  · 单向 —— 子代理不能反问用户或主代理，缺信息就按显式假设继续；
  · 结果截断 —— 超长结论按上限截断并显式标注，避免「隔离了个寂寞」；
  · 取消传导 —— 父任务停，子任务一起停（抛 Cancelled，让主循环按取消处理）；
  · 超轮次 / 超时 / 模型报错 → 结构化失败文本，不炸掉父任务。

这一层刻意不碰 UI、不写事件流、不落会话：子代理的过程对用户不可见，
主对话只应看到一行「子任务已完成 / 失败」与它的结论。
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass

from . import cancel
from .agent import Agent
from .registry import REGISTRY, Tool, ToolRegistry

#: 子代理结果上限（字符）。超过就截断并标注 —— 结论超过这个长度，
#: 说明它把过程也搬回来了，隔离的收益已经没了。
MAX_RESULT_CHARS = 2000

#: 子代理默认轮次上限：只做一件事，不需要主代理那么多轮；
#: 但也不能太紧 —— 实测 6 轮时，一个「统计文件数」的子任务会撞上限、
#: 只交出「未完成」的收尾摘要，父代理还得自己复核一遍。
DEFAULT_MAX_TURNS = 10

#: 默认墙钟超时（秒）。超时由 watchdog 调 interrupt() 中断，不依赖工具自己配合。
DEFAULT_TIMEOUT = 180.0

#: 子代理可用的工具：**只读**白名单。
#: 写文件 / 跑命令 / 改 git / 杀进程 / 记记忆 / 提问授权 一律不给 ——
#: 子代理的副作用应当由主代理显式执行，出问题时才好定位是谁改的。
#: search_tools 也不给：它会绕过白名单往本会话里加载任意工具。
SUBAGENT_TOOL_NAMES = frozenset({
    # 文件与代码（只读）
    "read_file", "list_dir", "glob_files", "grep", "file_info", "diff_files",
    "count_lines", "count_code_lines", "search_symbol", "detect_encoding", "file_hash",
    # 文档与表格（只读）
    "read_csv", "read_json", "read_jsonl", "read_xlsx", "read_docx",
    "read_pdf", "read_pdf_tables", "docx_to_markdown", "py_syntax_check",
    "view_image", "ocr_image", "ocr_pdf", "list_archive", "image_info",
    # 联网
    "web_search", "fetch_url",
    # 文本 / 数据 / 时间
    "html_to_text", "json_query", "url_decode", "url_encode", "case_convert",
    "text_hash", "base64_decode", "base64_encode", "word_freq",
    "csv_sql", "df_summary", "now", "time_add", "time_diff", "convert_time",
    # 系统（只看不改）
    "system_info", "disk_usage", "disk_overview", "cpu_usage", "memory_usage",
    "list_processes", "port_usage", "network_rate", "machine_status",
    "system_uptime", "top_cpu_processes", "env_info",
    # git（只读子命令）
    "git_status", "git_log", "git_diff", "git_branch",
})

#: 子代理的系统提示词。刻意与主提示词分开：它不需要人格设定、记忆、技能清单，
#: 只需要「怎么把一件事做完并把结论说清楚」。
SUBAGENT_SYSTEM_PROMPT = """\
你是主智能体派出的子任务执行者，只对本次任务负责。

工作方式：
1. 你只看到任务包，看不到主对话历史。任务包没写清的信息，按最合理的显式假设继续，
   并在结论里注明假设是什么 —— 不要停下来反问，也没有人给你补充。
2. 只做任务要求的事，不要顺手扩大范围（顺手改文件、顺手装依赖都属于越界）。
3. 工具以只读为主；确实缺能力时，说明「需要什么能力、为什么」，不要硬凑。
4. 轮次预算：工具调用要少而准（一次读多个文件、一次正则在多处匹配）。
   用掉大约一半轮次还没收敛，就立刻按下面三段把现有结论定稿 ——
   你最后一条消息就是交给主代理的东西，不要停在探索中途。

输出格式（固定三段，不要加寒暄和标题层级）：
- 结论：一句话说清结果。
- 证据：支撑结论的关键位置（文件:行号 / 命令输出要点 / 链接），最多 6 条。
- 未决：没解决的问题或你做的假设；没有就写「无」。
整体控制在 800 字以内，不要贴大段原文或整段日志。
"""


@dataclass
class SubagentResult:
    """子任务结果。`str(result)` 就是可以直接回填给主代理的文本。"""

    ok: bool
    text: str
    turns: int = 0
    truncated: bool = False

    def __str__(self) -> str:
        return self.text


def build_registry(source: ToolRegistry | None = None) -> ToolRegistry:
    """按只读白名单克隆一份工具表；不会改动 source（通常是全局 REGISTRY）。"""
    src = source if source is not None else REGISTRY
    reg = ToolRegistry()
    for t in src.all():
        if t.name in SUBAGENT_TOOL_NAMES:
            reg.add(t)
    return reg


def _clip(text: str) -> tuple[str, bool]:
    s = (text or "").strip()
    if len(s) <= MAX_RESULT_CHARS:
        return s, False
    cut = len(s) - MAX_RESULT_CHARS
    return s[:MAX_RESULT_CHARS] + f"\n…（已截断 {cut} 字：结论超长说明过程被搬回来了）", True


def run_subagent(
    task: str,
    *,
    client=None,
    registry: ToolRegistry | None = None,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout: float | None = DEFAULT_TIMEOUT,
    parent_token: cancel.CancellationToken | None = None,
) -> SubagentResult:
    """跑一个子任务并返回结论。

    · 父任务取消 → 抛 Cancelled（与普通工具一致，主循环据此收尾）；
    · 超时 / 超轮次 / 模型异常 → 返回 ok=False 的文本，让主代理自己决定重试或换路。
    """
    text = (task or "").strip()
    if not text:
        return SubagentResult(False, "[error] 子任务描述为空：请写清目标、输入路径与验收标准。")

    parent = parent_token if parent_token is not None else cancel.current_token()
    if parent is not None and parent.cancelled():
        raise cancel.Cancelled(parent.reason)

    agent = Agent(
        model=model,
        registry=registry if registry is not None else build_registry(),
        system_prompt=SUBAGENT_SYSTEM_PROMPT,
        max_turns=max_turns,
        client=client,
        verbose=False,
    )
    # session_id 留空：不落会话、不触发记忆提炼、不写事件流 —— 过程对主对话不可见。

    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        agent.interrupt("子任务超时")

    timer = None
    if timeout and timeout > 0:
        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        timer.start()

    # 父任务取消时把子代理一起停掉：子代理自己不知道主对话发生了什么
    if parent is not None:
        parent.on_cancel(lambda: agent.interrupt("父任务已取消"))

    try:
        answer = agent.run(text)
    except cancel.Cancelled:
        if timed_out.is_set():
            return SubagentResult(
                False, f"[error] 子任务超时（{timeout:.0f}s 未完成），已中止。", agent.usage["llm_calls"]
            )
        raise
    except Exception as e:  # noqa: BLE001 —— 子任务失败不该炸掉父任务
        return SubagentResult(
            False, f"[error] 子任务执行失败（{type(e).__name__}: {e}）", agent.usage["llm_calls"]
        )
    finally:
        if timer is not None:
            timer.cancel()

    clipped, truncated = _clip(answer)
    return SubagentResult(True, clipped, agent.usage["llm_calls"], truncated)


# ---------------------------------------------------------------- 触发规则

#: 任务描述少于这个长度就认为没写清（「看看这个项目」这类任务派出去也是浪费）
MIN_TASK_CHARS = 8

#: 上下文占用超过这个比例时，把「隔离出去」作为委派的理由写进返回值
CONTEXT_PRESSURE = 0.6

#: 本轮上下文的占用（字符数, 上限）。由 Agent 每回合刷新；子代理运行期间会被
#: 它自己的小上下文覆盖，父代理下一回合再写回来 —— 这里只用于给「要不要隔离」
#: 提供参考，不参与任何安全判断，所以这种短暂覆盖可以接受。
_PARENT_CTX: ContextVar[tuple[int, int]] = ContextVar("subagent_parent_ctx", default=(0, 0))


def set_parent_context(chars: int, limit: int) -> None:
    _PARENT_CTX.set((int(chars or 0), int(limit or 0)))


def parent_context() -> tuple[int, int]:
    return _PARENT_CTX.get()


def should_delegate(
    task: str,
    *,
    depth: int = 0,
    context_chars: int | None = None,
    context_limit: int | None = None,
) -> tuple[bool, str]:
    """委派前的硬约束检查。**只做否决**，不替模型决定「该不该拆」。

    返回 (是否允许, 理由)。允许但上下文吃紧时理由非空 —— 调用方可以把它
    写进结果，帮模型理解为什么这一步值得隔离。
    """
    text = (task or "").strip()
    if depth >= 1:
        return False, "子代理不能再派子代理（深度上限 1）"
    if len(text) < MIN_TASK_CHARS:
        return False, "任务描述过于笼统：请写清目标、输入路径与验收标准"

    chars, limit = parent_context()
    if context_chars is not None:
        chars = int(context_chars)
    if context_limit is not None:
        limit = int(context_limit)
    if limit > 0 and chars > limit * CONTEXT_PRESSURE:
        return True, f"当前上下文已用 {chars}/{limit} 字符，隔离出去能省下中间过程"
    return True, ""


def _limits() -> tuple[int, float]:
    """从通用配置读子任务轮次 / 超时；配置层异常按内置默认值处理。"""
    max_turns, timeout = DEFAULT_MAX_TURNS, DEFAULT_TIMEOUT
    try:
        from . import settings

        max_turns = int(settings.get("subagent_max_turns") or DEFAULT_MAX_TURNS)
        timeout = float(settings.get("subagent_timeout") or DEFAULT_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass
    return max_turns, timeout


def delegate(task: str) -> str:
    """delegate 工具的实现：规则检查 → 跑子任务 → 把结论转成工具文本。"""
    ok, reason = should_delegate(task)
    if not ok:
        return f"[skipped] 未委派：{reason}。请自己完成这个任务。"

    max_turns, timeout = _limits()
    result = run_subagent(
        task,
        max_turns=max_turns,
        timeout=timeout,
        parent_token=cancel.current_token(),   # 父任务停止时子任务一起停
    )
    if not result.ok:
        return result.text

    note = "（结论超长已截断）" if result.truncated else ""
    head = f"[ok] 子任务完成，用时 {result.turns} 轮{note}"
    if reason:
        head += f"；{reason}"
    return f"{head}\n{result.text}"


DELEGATE_TOOL = Tool(
    name="delegate",
    description=(
        "长调查先派出去：当一件事预计要读多个文件 / 翻多页文档 / 反复检索才能得出一个结论"
        "（大致 ≥6 轮工具调用，或中间内容 ≥2 万字符），先用本工具把这一步整体交给子代理，"
        "你只拿结论 —— 不要自己一轮轮查完、把过程留在当前对话里。"
        "同一目标下互不依赖的分支，可以在同一轮里并发调多次。"
        "【什么时候别用】两三次工具就能答完的；需要边做边和用户确认的；需要写文件 / "
        "执行命令 / 改 git 的（子代理是只读的，这类事请你自己做）。"
        "【task 必须自包含】子代理看不到当前对话、也不会反问你，请写清：目标、输入"
        "（文件路径 / URL / 关键词）、约束、验收标准、期望输出的要点。"
        "结论超过 2000 字会被截断。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "任务包：目标 + 输入路径 + 约束 + 验收标准 + 期望输出，"
                    "写成一段可以直接交给陌生同事执行的话"
                ),
            },
        },
        "required": ["task"],
    },
    func=delegate,
    group="协作",
    dangerous=False,
    source="core",
    module="core.subagent",
)


def register(reg: ToolRegistry | None = None) -> None:
    """把 delegate 注册进指定注册表（默认全局 REGISTRY）。幂等。"""
    target = reg if reg is not None else REGISTRY
    if target.get("delegate") is not None:
        return
    target.add(DELEGATE_TOOL)
