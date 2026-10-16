"""
Agent 核心循环：整个「智能体」最本质的部分。

一句话概括：
    把消息发给模型 → 若模型要调工具，就执行工具并把结果塞回消息 → 再来一轮。
    直到模型不再要工具，返回最终回答。

真实 Agent 与 demo 的差别，几乎全部集中在下面这几个地方：
  · 流式输出（_stream：把 SSE 增量拼回完整消息）
  · 并行工具执行（_run_tools：同一轮的多个工具并发跑，但结果按原顺序回填）
  · 真正的取消（run：令牌 + 杀子进程 + 关客户端 + 消息补齐 + 告知对端与用户，
    见 core/cancel.py、core/mcp.py 的 notify_cancelled）
  · 上下文控制（_trim_context）
  · 工具结果截断（core.sandbox.clip）
  · 异常兜底（工具报错不中断循环）
  · 危险操作确认（ui.ask_confirm，永远在主线程串行做）
  · 轮次上限（防止模型死循环）

关于「工具」：本循环不关心工具从哪来 —— 内置的、你写的插件、还是 MCP 外部服务，
在 `self.registry` 里都只是同一个 Tool 对象。加工具不需要改这个文件，一行都不用。

设计说明：openai 库采用懒加载，`client=` 可注入任意客户端，
因此循环逻辑可以脱离真实 API 单独测试（见 test_loop.py）。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# 环境兜底：部分 Windows 机器上，openai 3.x 的传输链路（httpx2 → httpcore2
# → truststore）首次加载 _ctypes 会报「DLL load failed: 句柄无效」；
# 趁进程还干净时先把 ctypes 加载进 sys.modules，truststore 之后直接走缓存。
import ctypes  # noqa: F401

try:  # 懒加载：没装 openai 也能跑工具层和循环测试
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

import tools  # noqa: F401  —— 导入即加载插件；同时保留 agent.tools 的旧用法
from core.cancel import CancellationToken, Cancelled, current_token, set_current_token
from core.images import take_images
from core.registry import REGISTRY, Tool, ToolRegistry
from core.ui import PlainUI

DEFAULT_SYSTEM_PROMPT = """你是一个能操作本地环境的智能体。

工作原则：
1. 先侦察再动手：不确定文件格式时，先用 run_shell 看魔数（xxd/head）或用 run_python 探测，不要凭扩展名猜。
2. 工具报错不是终点：换一种方式重试，或先补环境（如装依赖）再重试。
3. 结论必须有证据：给出结论时附上可复核的数字（大小、哈希、计数、行号）。
4. 大输出自己收敛：不要在回复里粘贴超长原始数据，只保留关键片段。
5. 不确定就问，不要臆测用户意图；涉及删除/覆盖等破坏性操作前先说明。

如果现有工具不足以完成任务，明确说明缺什么能力，而不是硬凑。
回答用简体中文。"""

# 取消时给模型看的统一占位文本（也是给用户看的轨迹内容）
CANCELLED_TEXT = "[cancelled] 用户取消了本次任务，该工具未执行完成。"


def _setting(name: str):
    """读一个通用配置项（Web UI「通用配置」可改）；配置层出问题就返回 None。"""
    try:
        from core import settings

        return settings.get(name)
    except Exception:  # noqa: BLE001 —— 配置层异常不该让 Agent 起不来
        return None


def _pick(explicit, name: str, fallback):
    """三级取值：显式参数 > 通用配置 > 内置兜底。"""
    if explicit is not None:
        return explicit
    value = _setting(name)
    return fallback if value is None else value


def _resolve_model() -> str:
    """当前生效的模型名：优先用户在 Web UI 里选的，其次环境变量，最后兜底。"""
    try:
        from core import model_config

        m = model_config.resolve_active().get("model")
        if m:
            return m
    except Exception:  # noqa: BLE001 —— 配置层出问题不该让 Agent 起不来
        pass
    return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _resolve_endpoint() -> tuple[str, str]:
    """当前生效的 (base_url, api_key)：用户配置优先，回退环境变量。"""
    try:
        from core import model_config

        cfg = model_config.resolve_active()
        return cfg.get("base_url") or "", cfg.get("api_key") or ""
    except Exception:  # noqa: BLE001
        return os.environ.get("OPENAI_BASE_URL", ""), os.environ.get("OPENAI_API_KEY", "")


def _flatten_history(messages: list[dict[str, Any]]) -> str:
    """把消息列表压成纯文本，供摘要器阅读（工具结果截断，避免摘要输入本身超长）。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            lines.append(f"用户：{content if isinstance(content, str) else ''}")
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            names = ",".join(
                str((c.get("function") or {}).get("name") or "") for c in calls
            )
            body = content if isinstance(content, str) else ""
            if body:
                lines.append(f"助手：{body}")
            if names:
                lines.append(f"助手调用了工具：{names}")
        elif role == "tool":
            text = content if isinstance(content, str) else ""
            lines.append(f"工具结果：{text[:400]}")
    return "\n".join(lines)


class Agent:
    def __init__(
        self,
        model: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_turns: int | None = None,
        temperature: float | None = None,
        verbose: bool = True,
        max_context_chars: int | None = None,
        client: Any = None,                 # 注入点：测试时传假客户端
        registry: ToolRegistry | None = None,
        ui: Any = None,                     # 注入点：控制台传 RichUI
        approve: bool = False,              # 危险工具是否要人工确认
        keep_recent_turns: int | None = None,  # 滑动窗口保留轮数
        stream: bool | None = None,         # None = 读通用配置（默认开）
        parallel_tools: bool | None = None, # None = 读通用配置（默认开）
        max_workers: int | None = None,     # None = 读通用配置（默认 4）
    ) -> None:
        self.model = model or _resolve_model()
        self.system_prompt = system_prompt
        # 显式参数 > 通用配置（Web UI 可改）> 内置默认
        self.max_turns = int(_pick(max_turns, "max_turns", 25))
        self.temperature = float(_pick(temperature, "temperature", 0.0))
        self.verbose = verbose
        self.max_context_chars = int(_pick(max_context_chars, "max_context_chars", 120_000))
        # 滑动窗口：至少保留最近这么多轮（1 轮 = 1 条 user 及其后续）
        self.keep_recent_turns = max(1, int(_pick(keep_recent_turns, "keep_recent_turns", 8)))
        # 不能写 `registry or REGISTRY`：ToolRegistry 有 __len__，空注册表是 falsy
        self.registry = registry if registry is not None else REGISTRY
        self.ui = ui if ui is not None else PlainUI(enabled=verbose)
        self.approve = approve
        self.stream = bool(_pick(stream, "stream", True))
        self.parallel_tools = bool(_pick(parallel_tools, "parallel_tools", True))
        self.max_workers = max(1, int(_pick(max_workers, "max_workers", 4)))
        self._client = client
        self._owns_client = client is None   # False = 外部注入的客户端，取消时只关不丢
        self._stream_seen = 0     # 本轮流式已收到的帧数：>0 表示不能安全重试
        self.cancel_token: CancellationToken | None = None  # 每次 run() 新建

        # 上一轮 run() 的最终回答是否已经「边生成边打印」过了。
        # 控制台/一次性模式据此决定要不要再 print 一遍，避免同一段文字出现两次。
        self.last_answer_streamed = False

        # 取消提示是否已经由 UI 层显示过（同上：避免控制台重复打印）。
        self.last_cancel_notice_shown = False

        # 运行统计：真实生产环境里这几项决定成本与延迟，值得默认采集
        self.usage: dict[str, int] = {
            "llm_calls": 0, "tool_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }

        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    # ------------------------------------------------------------ LLM 客户端

    def _get_client(self):
        """懒加载：真正要调用模型时才构造客户端。"""
        if self._client is None:
            if OpenAI is None:
                raise RuntimeError("未安装 openai 库，请先执行 pip install -r requirements.txt")
            base_url, api_key = _resolve_endpoint()
            self._client = OpenAI(
                api_key=api_key or None,
                base_url=base_url or None,
            )
            self._owns_client = True
        return self._client

    # ------------------------------------------------------------ UI 保护层

    def _ui_call(self, name: str, *args, **kwargs):
        """调用 UI 方法，但永远不让 UI 的问题影响循环。

        为什么需要这一层？
          1. UI 是可选能力（自定义 UI 可以只实现一部分方法），缺失时应静默跳过；
          2. 控制台读写终端时可能抛异常（管道关闭等），不该因此中断整个任务；
          3. 自定义 UI 可能用的是旧签名（没有 streamed= 这类新参数），
             这时去掉关键字参数再试一次，免得逼所有人跟着核心升级。
        """
        fn = getattr(self.ui, name, None)
        if fn is None:
            return None
        try:
            return fn(*args, **kwargs)
        except TypeError:
            if not kwargs:
                return None
            try:
                return fn(*args)
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ 上下文管理

    def _context_chars(self) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in self.messages)

    def _prefix_end(self) -> int:
        """system（含滚动摘要）之后的下标 —— 摘要消息固定在第 1 位。"""
        if len(self.messages) > 1 and self.messages[1].get("_summary"):
            return 2
        return 1

    def _has_summary(self) -> bool:
        return len(self.messages) > 1 and bool(self.messages[1].get("_summary"))

    def _turn_starts(self) -> list[int]:
        """每轮对话的起点下标（user 消息所在位置）。"""
        return [i for i, m in enumerate(self.messages) if m.get("role") == "user"]

    def _recent_start(self, keep: int) -> int:
        """最近 keep 轮的起点下标；轮数不足 keep 时返回 -1（表示无需裁剪）。"""
        starts = self._turn_starts()
        if len(starts) <= keep:
            return -1
        return starts[-keep]

    def _trim_context(self) -> None:
        """上下文过长时的三级收敛：便宜的先做，贵的后做。

        1) 把较早的工具结果替换成占位符 —— 零成本，优先；
        2) 仍超限：把「摘要之后、最近 N 轮之前」的历史滚动摘要成一段话，
           保留任务目标/决策/进度/待办，关键信息不丢；
        3) 仍超限：滑动窗口硬截断，只留 system + 摘要 + 最近 N 轮，
           确保永远不超窗口、接口不报错。

        裁剪一律以「轮」为单位（轮起点是 user 消息），这样 assistant 的
        tool_calls 与其 tool 结果要么一起留、要么一起删，不会出现配对错位。
        """
        if self._context_chars() <= self.max_context_chars:
            return

        self._trim_old_tool_results()
        if self._context_chars() <= self.max_context_chars:
            return

        self._roll_summary()
        if self._context_chars() <= self.max_context_chars:
            return

        self._hard_window()

    def _trim_old_tool_results(self) -> None:
        """第一级：把较老的工具输出换成占位符，保住对话主线的文字。"""
        protected = 6
        for i, m in enumerate(self.messages):
            if i >= len(self.messages) - protected:
                break
            if m.get("role") == "tool" and not m.get("_trimmed"):
                m["content"] = "…[较早的工具结果已省略以节省上下文]…"
                m["_trimmed"] = True
                if self._context_chars() <= self.max_context_chars:
                    return

    def _roll_summary(self) -> None:
        """第二级：把较早的整轮历史摘要成一段，替换掉原文。"""
        boundary = self._recent_start(self.keep_recent_turns)
        if boundary < 0:
            return
        prefix_end = self._prefix_end()
        if boundary <= prefix_end:
            return

        old = self.messages[prefix_end:boundary]
        prev = self.messages[1]["content"] if self._has_summary() else ""
        summary = self._summarize(_flatten_history(old), prev)
        if not summary:
            return      # 摘要失败（网络/模型异常）→ 交给第三级硬截断，绝不阻塞主流程

        msg = {"role": "system", "content": "[历史摘要] " + summary, "_summary": True}
        # 旧摘要已被合并进新摘要，这里整体重建
        self.messages = self.messages[:1] + [msg] + self.messages[boundary:]

    def _hard_window(self) -> None:
        """第三级：滑动窗口硬截断，保留 system + 摘要 + 最近 N 轮。"""
        boundary = self._recent_start(self.keep_recent_turns)
        if boundary < 0:
            return
        prefix_end = self._prefix_end()
        if boundary <= prefix_end:
            return
        self.messages = self.messages[:prefix_end] + self.messages[boundary:]

    def _summarize(self, text: str, prev: str) -> str | None:
        """调当前模型把历史压成摘要；任何失败都返回 None（不阻断任务）。"""
        if not text.strip():
            return None
        tok = self.cancel_token
        try:
            if tok is not None:
                tok.check()
            client = self._get_client()
            system = (
                "你是对话压缩器。把给定的历史对话压缩成简洁的中文摘要，"
                "只保留：任务目标、关键决策、已完成步骤、待办事项、"
                "重要文件路径与数据结论。不要编造原文没有的信息。"
            )
            user = (f"已有摘要：\n{prev}\n\n" if prev else "") + f"待压缩的历史：\n{text}"
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
            )
            out = (resp.choices[0].message.content or "").strip()
            return out or None
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001 —— 摘要只是优化，失败不该影响任务
            return None

    def context_stats(self) -> dict:
        """给控制台 /context 命令用。"""
        return {
            "messages": len(self.messages),
            "chars": self._context_chars(),
            "limit": self.max_context_chars,
            "trimmed": sum(1 for m in self.messages if m.get("_trimmed")),
            "summarized": self._has_summary(),
            "keep_recent_turns": self.keep_recent_turns,
        }

    # ------------------------------------------------------------ 主循环

    def run(self, user_input: str) -> str:
        """运行一个任务。被取消时抛 core.cancel.Cancelled（不抛 KeyboardInterrupt）。

        取消的三条触发路径，最后都归一化成 Cancelled：
          1. 协作式：任何检查点（轮首 / 流式分片间 / 工具执行前 / MCP 等待中）
             发现令牌已取消 → 抛 Cancelled；
          2. Ctrl+C：KeyboardInterrupt 落在主线程 → 先 token.cancel()
             （杀子进程、关客户端），再转成 Cancelled；
          3. 外部强杀：cancel_token.cancel()（令牌钩子关掉 HTTP 客户端，
             卡住的网络读会报错 → 路径 1 的兜底把它接住转成 Cancelled）。

        不论走哪条路径，收尾都做同样三件事，缺一不可：
          · token 已取消（补一次，保证子进程/网络句柄被清理）；
          · 补齐缺失的 tool 回复（消息序列仍然合法，下一轮还能发出去）；
          · 通过 UI 把「为什么取消」告诉人（repl/main 靠
            last_cancel_notice_shown 判断要不要再打一遍）。
        """
        token = CancellationToken()
        self.cancel_token = token
        self.last_cancel_notice_shown = False
        prev = set_current_token(token)
        # 钩子：取消时关闭 HTTP 客户端，卡在 socket 读上的请求会立刻报错 ——
        # 否则「非流式 + 网络卡住」时 Ctrl+C 打不断，取消就是一句空话。
        # 无条件注册：客户端是 _run_loop 里懒创建的，首次 run() 时还是 None，
        # 只在「已有客户端」时注册会让首轮任务的取消钩子悄悄失效；
        # _abort_client 自己会处理 client is None 的情况。
        token.on_cancel(self._abort_client)
        try:
            return self._run_loop(user_input)
        except Cancelled as e:
            if not token.cancelled():
                # 协作式取消理论上都伴随一次 token.cancel()；这里兜住
                # 「某个插件自己抛了 Cancelled」这种情况，别让清理被跳过。
                token.cancel(str(e) or "任务被取消")
            self._patch_cancelled_replies()
            self.last_cancel_notice_shown = bool(
                self._ui_call("cancelled", token.reason)
            )
            raise
        except KeyboardInterrupt:
            reason = "用户按下 Ctrl+C"
            token.cancel(reason)
            self._patch_cancelled_replies()
            self.last_cancel_notice_shown = bool(self._ui_call("cancelled", reason))
            raise Cancelled(f"任务已被用户取消（{reason}）") from None
        finally:
            self.cancel_token = None
            set_current_token(prev)

    def interrupt(self, reason: str = "程序化要求中断当前任务") -> None:
        """程序化取消当前任务（等价于 Ctrl+C 的效果）。没有任务在跑则无事发生。

        reason 会被带到 MCP 的取消通知与 UI 提示里 —— 让「谁取消的、为什么」
        一路留痕，而不是最后只留下一句无信息量的「已取消」。
        """
        if self.cancel_token is not None:
            self.cancel_token.cancel(reason)

    def _abort_client(self) -> None:
        """取消钩子：关闭 HTTP 客户端，让卡在 socket 读上的请求立刻报错。

        只丢弃自己创建的客户端（下次 run() 重建一个干净的）；
        外部注入的客户端（测试假件、调用方自带的连接池）只调 close()、
        所有权归注入方，不能替人家置空。
        """
        client = self._client
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        if self._owns_client:
            self._client = None

    def _patch_cancelled_replies(self) -> None:
        """取消后补齐缺失的 tool 回复，保证消息序列仍然合法。

        OpenAI 协议要求：assistant 消息里每个 tool_call 都必须有对应的
        tool 消息。取消往往发生在「工具执行到一半」，如果不补，下一条
        用户消息发过去就会被 API 拒绝。补上的内容明确告诉模型：
        这些工具没跑完，别基于它们下结论。
        """
        last: dict[str, Any] | None = None
        for m in reversed(self.messages):
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                last = m
                break
            if role == "user":
                break   # 更早的轮次早就补齐了，不用管
        if last is None:
            return
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in last["tool_calls"]:
            if tc["id"] not in answered:
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": CANCELLED_TEXT,
                    }
                )

    def _run_loop(self, user_input: str) -> str:
        take_images()   # 丢弃上一次任务可能残留的附件（如取消时没取走的）
        self.messages.append({"role": "user", "content": user_input})
        client = self._get_client()
        schemas = self.registry.schemas()
        self.last_answer_streamed = False

        for turn in range(1, self.max_turns + 1):
            tok = self.cancel_token
            if tok is not None:
                tok.check()
            self._trim_context()
            self._ui_call("turn_start", turn)

            t0 = time.time()
            payload = {
                "model": self.model,
                # 过滤内部字段（_trimmed 等），只把合法消息发给接口
                "messages": [
                    {k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages
                ],
                "tools": schemas,
                "tool_choice": "auto",
                "temperature": self.temperature,
            }
            # 取消钩子关掉客户端时，卡住的请求会报各种异常（ReadError/
            # RuntimeError…）—— 由 _request_or_cancel 统一转成 Cancelled。
            resp = self._request_or_cancel(client, payload, tok)

            # 流式与普通模式的分叉点。
            # 判定用 __next__ 而不是 __iter__：openai 的 ChatCompletion 是 pydantic 模型，
            # 它**也**实现了 __iter__，用 __iter__ 判断会把普通响应误当成流。
            # 可靠的区别是「流是迭代器（有 __next__），响应对象不是」。
            shown = False
            if self.stream and hasattr(resp, "__next__"):
                try:
                    got = self._consume_stream_safe(turn, resp)
                except Exception:
                    # 取消钩子关掉客户端后，卡在流读取上的请求会抛各种传输异常
                    # （APIConnectionError/ReadError…）。令牌已取消时它们都是
                    # 「取消的表现形式」，统一归一化成 Cancelled —— 否则
                    # 程序化取消（repl /stop、Web UI 停止按钮）会被误报成错误。
                    if tok is not None and tok.cancelled():
                        raise Cancelled("LLM 流式响应被取消") from None
                    raise
                if got is None:
                    # 服务端「答应了流式」却一帧都不发（网关、代理、自研服务端都干得出来）。
                    # 因为一帧都没收到，重试既不会重复计费，也不会丢内容。
                    self._ui_call(
                        "notice", "[yellow]· 流式响应为空，本轮自动改用普通模式重试[/yellow]"
                    )
                    if tok is not None:
                        tok.check()   # 别把「取消导致的空流」误判成服务端问题
                    self.stream = False   # 别每轮都白试一次
                    t0 = time.time()
                    resp = self._request_or_cancel(client, payload, tok)
                    content, tool_calls, usage = self._consume(resp)
                else:
                    content, tool_calls, usage, shown = got
            else:
                content, tool_calls, usage = self._consume(resp)
            latency = time.time() - t0

            # 消费完再查一次：取消可能恰好落在「最后一个分片之后」——
            # 此时流正常结束，不做这个检查的话，取消会被静默吞掉。
            if tok is not None and tok.cancelled():
                raise Cancelled("任务已被用户取消")

            self._account(usage)
            self._ui_call("llm_done", turn, latency, streamed=shown)

            # 把助手消息（可能带 tool_calls）记进历史
            entry: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            self.messages.append(entry)

            # 没有工具调用 → 本轮就是最终答案
            if not tool_calls:
                self.last_answer_streamed = shown
                return content or ""

            # 有工具调用 → 执行（可能并行），结果按原顺序回填
            for tc, result in self._run_tools(turn, tool_calls):
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": result,
                    }
                )

            # 工具可能通过附件通道递了图片（如 view_image）：
            # 回填完工具结果后取走，作为一条 user 消息追加，
            # 视觉模型在下一轮就能真正「看到」这些图。没有附件时什么都不做。
            images = take_images()
            if images:
                self.messages.append({"role": "user", "content": images})

        return f"[达到最大轮次 {self.max_turns}，已停止。]"

    # ------------------------------------------------------------ 请求与解析

    def _request_or_cancel(self, client, payload: dict, token: CancellationToken | None) -> Any:
        """发请求并把「取消钩子导致的异常」统一归一化成 Cancelled。"""
        try:
            return self._request(client, payload)
        except Exception:  # noqa: BLE001
            if token is not None and token.cancelled():
                raise Cancelled("LLM 请求被取消") from None
            raise

    def _request(self, client, payload: dict):
        """发送请求。返回流对象或普通响应对象。

        流式降级的两级兜底（这两条都是踩过才知道要写的）：
          1. 有些服务端不认识 `stream_options`（用它才能在流式里拿到 usage）
             → 去掉它重试，代价只是流式模式没有 token 统计；
          2. 有些服务端根本不支持 `stream`
             → 退回普通模式，功能不受影响，只是没有逐字效果。
        真实的报错（鉴权失败、限流、模型名写错）不会被吞掉，照常抛出。
        """
        if not self.stream:
            return client.chat.completions.create(**payload, stream=False)

        try:
            return client.chat.completions.create(
                **payload, stream=True, stream_options={"include_usage": True}
            )
        except Exception as e:  # noqa: BLE001
            text = str(e).lower()
            if "stream_options" in text:
                self._ui_call("notice", "[dim]· 服务端不支持 stream_options，流式模式将不统计 token[/dim]")
                return client.chat.completions.create(**payload, stream=True)
            unsupported = any(k in text for k in ("unsupported", "not support", "unknown", "unrecognized", "invalid"))
            if "stream" in text and unsupported:
                self._ui_call("notice", "[yellow]· 服务端不支持流式输出，已退回普通模式[/yellow]")
                self.stream = False
                return client.chat.completions.create(**payload, stream=False)
            raise

    def _consume(self, resp) -> tuple[str, list[dict], Any]:
        """普通（非流式）响应 → (内容, tool_calls, usage)。"""
        msg = resp.choices[0].message
        tool_calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in (msg.tool_calls or [])
        ]
        return msg.content or "", tool_calls, getattr(resp, "usage", None)

    def _consume_stream_safe(self, turn: int, stream):
        """包一层「流是不是白拿了」的判定。

        返回 None 表示可以安全重试，两种情况：
          · 一帧都没收到就报错（服务端压根没按 SSE 回）
          · 一帧都没收到就结束了（SDK 对非 SSE 响应静默返回空流，不会抛异常）
        已经收到内容之后再报错，那是真错，必须照常抛出 —— 掩盖它只会更难查。
        """
        self._stream_seen = 0
        try:
            result = self._consume_stream(turn, stream)
        except Cancelled:
            raise   # 取消不是「空流」—— 立即上抛，绝不能掉进重试逻辑
        except Exception:
            if self._stream_seen:
                raise
            return None
        return result if self._stream_seen else None

    def _consume_stream(self, turn: int, stream) -> tuple[str, list[dict], Any, bool]:
        """流式响应 → (内容, tool_calls, usage, 是否已上屏)。

        流式协议下，一个逻辑上的 tool_call 会被拆成很多个分片：
            {"index":0,"id":"call_x","function":{"name":"read_file","arguments":""}}
            {"index":0,"function":{"arguments":"{\"pa"}}
            {"index":0,"function":{"arguments":"th\": \"a.txt\"}"}}
        所以必须按 `index` 聚合，把 arguments 逐片拼接 —— 这是流式模式下最容易写错的地方，
        拼错的表现是「参数不合法」或「工具名是空的」。
        """
        parts: list[str] = []
        acc: dict[int, dict[str, str]] = {}
        usage = None
        shown = False

        for chunk in stream:
            # 每个分片之间都是检查点：取消时关掉流（断开连接，不再浪费下行流量）
            tok = self.cancel_token
            if tok is not None and tok.cancelled():
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                raise Cancelled("流式响应被取消")
            self._stream_seen += 1
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage = u          # usage 通常出现在最后一帧（choices 为空的纯统计帧）

            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue

            piece = getattr(delta, "content", None)
            if piece:
                parts.append(piece)
                if self._ui_call("llm_delta", turn, piece, "content"):
                    shown = True

            # 推理模型（如 deepseek-reasoner）会把思考过程放在 reasoning_content 里，
            # 它不是回答的一部分，只用来展示「模型在想什么」。
            think = getattr(delta, "reasoning_content", None)
            if think:
                self._ui_call("llm_delta", turn, think, "reasoning")

            for tc in getattr(delta, "tool_calls", None) or []:
                slot = acc.setdefault(getattr(tc, "index", 0) or 0, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    # name 只在第一片出现，重复覆盖会丢；arguments 是分片，必须累加
                    if getattr(fn, "name", None) and not slot["name"]:
                        slot["name"] = fn.name
                    frag = getattr(fn, "arguments", None)
                    if frag:
                        slot["args"] += frag

        tool_calls = [
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                # 参数可能是空串（模型没给参数）—— 补成 {}，否则解析会失败
                "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
            }
            for idx, slot in sorted(acc.items())
        ]
        return "".join(parts), tool_calls, usage, shown

    def _account(self, usage) -> None:
        """累积用量：不同服务商的 usage 字段可能缺失，全部做兜底。"""
        self.usage["llm_calls"] += 1
        if usage is None:
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.usage[key] += getattr(usage, key, 0) or 0

    # ------------------------------------------------------------ 工具执行

    def _run_tools(self, turn: int, tool_calls: list[dict]) -> list[tuple[dict, str]]:
        """执行一轮里的全部工具调用，返回 [(tool_call, 结果文本)]，顺序与入参一致。

        并行的边界划在「执行」这一步：
          · 参数解析、查工具、危险确认  → 主线程串行做（确认必须问人，不能并发弹窗）
          · 真正的工具执行             → 可以并发（子进程/IO/网络都能重叠）
          · 结果回填                   → 严格按 tool_calls 原顺序（协议要求一一对应）

        什么时候不并行：
          · 只有一个工具调用（起线程池是纯开销）
          · 用户关掉了 parallel_tools
          · 本批里出现了 exclusive 工具（它会改共享状态，必须独占）

        取消语义：
          · 批次开始前已取消 → 全部标 [cancelled]，抛 Cancelled；
          · 执行中途取消 → 令牌的钩子已把子进程杀掉（工具很快返回），
            未启动/未完成的调用补 [cancelled]，然后抛 Cancelled。
        """
        token = self.cancel_token
        if token is not None:
            token.check()
        self.usage["tool_calls"] += len(tool_calls)
        prepared = [self._prepare(tool) for tool in tool_calls]

        results: list[str | None] = [None] * len(prepared)
        todo: list[tuple[int, Tool, dict]] = []
        for i, (_tc, tool, args, preset) in enumerate(prepared):
            if preset is None:
                # _prepare 的约定：preset 为 None 时 tool 一定有值
                assert tool is not None
                todo.append((i, tool, args))
            else:
                results[i] = preset

        if not todo:
            return [(tc, results[i] or "") for i, (tc, _t, _a, _p) in enumerate(prepared)]

        serial = (
            not self.parallel_tools
            or len(todo) < 2
            or any(tool.exclusive for _i, tool, _a in todo)
        )

        try:
            if serial:
                for i, tool, args in todo:
                    results[i] = self._invoke(turn, tool, args)
            else:
                self._ui_call("tools_start", turn, [(t, a) for _i, t, a in todo])
                workers = max(1, min(self.max_workers, len(todo)))
                batch_t0 = time.time()
                longest = 0.0
                pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool")
                try:
                    futures = {
                        pool.submit(self._call_tool, tool, args): (i, tool)
                        for i, tool, args in todo
                    }
                    for fut in as_completed(futures):
                        i, tool = futures[fut]
                        try:
                            result, seconds = fut.result()
                        except Cancelled:
                            result, seconds = CANCELLED_TEXT, 0.0
                        except Exception as e:  # noqa: BLE001 —— _call_tool 不该抛，纯兜底
                            result, seconds = f"[error] {type(e).__name__}: {e}", 0.0
                        results[i] = result
                        longest = max(longest, seconds)
                        # 打印放在主线程：轨迹行不会互相插队，控制台也不用为并发加锁
                        self._ui_call("tool_end", turn, tool, result, seconds)
                        if token is not None and token.cancelled():
                            raise Cancelled("工具批次被取消")
                finally:
                    # cancel_futures：还没轮到的直接出队，不再启动新工具；
                    # 正在跑的会自然结束 —— 它们的子进程已被令牌杀掉，很快返回。
                    pool.shutdown(wait=False, cancel_futures=True)
                # 报告批次墙钟：这是衡量并行收益最直接的数字
                # （N 个工具各自 X 秒，串行要 N*X，并行只要接近最大的那个 X）
                self._ui_call("tools_done", turn, len(todo), time.time() - batch_t0, longest)
        except Cancelled:
            for i, r in enumerate(results):
                if r is None:
                    results[i] = CANCELLED_TEXT
            raise

        return [(tc, results[i] or "") for i, (tc, _t, _a, _p) in enumerate(prepared)]

    def _prepare(self, tc: dict) -> tuple[dict, Tool | None, dict, str | None]:
        """主线程串行做的前置检查。第四个元素非 None 表示「不用执行，直接用这个结果」。"""
        name = tc["function"]["name"]
        raw_args = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}

        tool = self.registry.get(name)
        if tool is None:
            available = ", ".join(self.registry.names()[:20])
            return tc, None, args, f"[error] 未知工具：{name}。当前可用工具：{available}"

        if self.approve and tool.dangerous:
            if not self._ui_call("ask_confirm", tool, args):
                return tc, tool, args, (
                    "[denied] 用户拒绝执行该操作。请改用不需要确认的方式，"
                    "或向用户说明为什么必须执行它。"
                )

        return tc, tool, args, None

    def _call_tool(self, tool: Tool, args: dict) -> tuple[str, float]:
        """真正执行（可能跑在工作线程里）。注册表内部已兜住所有异常。

        执行前先查取消令牌：排队还没轮到的调用在这里快速失败，
        不必等前面的工具跑完才发现整批都白跑。
        """
        token = self.cancel_token
        if token is not None:
            token.check()
        t0 = time.time()
        result = self.registry.call(tool.name, args)
        return result, time.time() - t0

    def _invoke(self, turn: int, tool: Tool, args: dict) -> str:
        """串行路径：打印轨迹 + 执行。"""
        self._ui_call("tool_start", turn, tool, args)
        result, seconds = self._call_tool(tool, args)
        self._ui_call("tool_end", turn, tool, result, seconds)
        return result

    def _execute(self, turn: int, name: str, args: dict) -> str:
        """执行单个工具，并把所有「意外」都收敛成一段文本。

        这是 `_run_tools` 的单调用形态，保留下来有两个用途：
        控制台的调试入口，以及 test_plugins.py 里对「危险工具确认」的直接测试。

        三种情形：
          1. 工具不存在       → 告诉模型有哪些可用，让它自己换
          2. 危险工具 + 开确认 → 询问用户，被拒则明确告诉模型"用户拒绝了"
          3. 正常执行         → 交给注册表（内部已兜住所有异常）
        """
        tc = {"id": f"manual_{turn}", "type": "function",
              "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
        _tc, tool, parsed, preset = self._prepare(tc)
        if preset is not None:
            return preset
        assert tool is not None
        return self._invoke(turn, tool, parsed)

    # ------------------------------------------------------------ 便捷方法

    def reset(self, keep_system: bool = True) -> None:
        self.messages = self.messages[:1] if keep_system else []
        self.last_answer_streamed = False
        for k in self.usage:
            self.usage[k] = 0
