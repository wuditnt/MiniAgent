"""
控制台呈现层。

为什么要单独抽一层？
    Agent 循环只应该关心「调用工具、拿到结果」；至于结果怎么显示给人类看，
    是控制台的事。把两者分开之后：
      · 跑测试时用 PlainUI(enabled=False)，一行输出都没有
      · 跑控制台时用 RichUI，有颜色、有耗时、有工具轨迹
      · 将来想做 Web / TUI，只要再实现一个 UI 类

UI 需要实现的方法（全部可选，缺失时自动降级）：
    notice(text)                      普通提示
    turn_start(turn)                  新一轮开始
    llm_delta(turn, text, kind)       流式增量；返回 True 表示真的显示出来了
    llm_done(turn, latency, streamed) 模型本轮的流结束
    tools_start(turn, items)          一批工具即将执行
    tool_start(turn, tool, args)      准备执行某个工具
    tool_end(turn, tool, result, sec) 工具执行完毕
    ask_confirm(tool, args) -> bool   危险操作的交互确认
    cancelled(reason) -> bool         任务被取消；返回 True 表示已提示过

关于线程安全：
    工具可能被并行执行（见 agent._run_tools），虽然目前打轨迹的是主线程，
    但 UI 仍然自带一把可重入锁 —— 任何人在任何线程里调用都不会把输出搅在一起。
"""

from __future__ import annotations

import json
import re
import sys
import threading
from typing import Any


def brief(value: Any, limit: int = 70) -> str:
    """把参数值压成单行短文本，避免把控制台刷屏。"""
    if isinstance(value, str):
        s = value.replace("\n", "⏎")
    else:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _raw_write(text: str) -> None:
    """绕过 rich 直接写 stdout —— 流式输出专用。

    为什么不用 console.print(text, end="")？
      1. rich 会把模型输出里的 [xxx] 当成标记解析，轻则吞字重则报错；
      2. rich 按「每次调用」做整段排版，逐块输出会把长行切得七零八落。
    宁可让流式内容朴素一点，也不要它变形。
    """
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:      # Windows 下 cp936 控制台遇到生僻字
        try:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
            sys.stdout.write(text)
        except Exception:  # noqa: BLE001
            return
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _ansi(code: str) -> str:
    """只在真实终端里输出 ANSI 颜色；重定向到文件时不污染内容。"""
    try:
        return code if sys.stdout.isatty() else ""
    except Exception:  # noqa: BLE001
        return ""


def _ask_hint(allow_free_text: bool, multi: bool) -> str:
    """终端提示语：随单选/多选与是否允许自由文本变化。"""
    base = "输入序号（多个用逗号分隔）" if multi else "输入序号"
    return base + "或文字" if allow_free_text else base


def _resolve_answer(ans: str, opts: list[str], allow_free_text: bool,
                    multi: bool = False) -> str:
    """把终端输入解析成答复。

    单选：纯数字且命中候选 → 该候选文本；否则按自由文本处理。
    多选：按逗号/顿号/空格拆开，逐项映射为候选文本，保序去重后用「、」拼回。
    """
    ans = (ans or "").strip()
    if not ans:
        return ""
    if not multi:
        if ans.isdigit() and opts:
            idx = int(ans)
            return opts[idx - 1] if 1 <= idx <= len(opts) else ""
        return ans if allow_free_text else ""

    picked: list[str] = []
    for part in re.split(r"[,，、\s]+", ans):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and opts:
            idx = int(part)
            if 1 <= idx <= len(opts):
                picked.append(opts[idx - 1])
        elif allow_free_text:
            picked.append(part)
    out: list[str] = []
    for x in picked:
        if x not in out:
            out.append(x)
    return "、".join(out)


class PlainUI:
    """最朴素的实现：纯 print。测试与管道场景用它。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.RLock()
        self._stream_kind: str | None = None   # None / content / reasoning

    # ---------------------------------------------------------- 基础

    def _p(self, text: str) -> None:
        if self.enabled:
            with self._lock:
                print(text)

    def notice(self, text: str) -> None:
        self._p(text)

    def turn_start(self, turn: int) -> None:
        pass

    # ---------------------------------------------------------- 流式

    def llm_delta(self, turn: int, text: str, kind: str = "content") -> bool:
        """流式增量上屏。返回 True 表示「这段文字已经显示过了」。

        返回值不是装饰 —— Agent 靠它决定最终回答还要不要再 print 一遍。
        如果 UI 被关掉（enabled=False）却仍返回 True，回答就会凭空消失。
        """
        if not self.enabled or not text:
            return False
        with self._lock:
            if kind != self._stream_kind:
                # 从「思考」切到「正文」（或者反过来）时补个换行，否则两段会黏在一起
                self._close_stream_line()
                if kind == "reasoning":
                    sys.stdout.write(_ansi("\033[2m") + "[思考] ")
                self._stream_kind = kind
            _raw_write(text)
        return kind == "content"

    def _close_stream_line(self, quiet: bool = False) -> None:
        """流式输出结束时补一个换行（内部调用，调用方需持锁）。"""
        if self._stream_kind is not None:
            if self._stream_kind == "reasoning":
                sys.stdout.write(_ansi("\033[0m"))
            if not quiet:
                _raw_write("\n")
            self._stream_kind = None

    def llm_done(self, turn: int, latency: float, streamed: bool = False) -> None:
        if self._stream_kind is not None:
            with self._lock:
                self._close_stream_line()
        self._p(f"\n[第 {turn} 轮] 模型返回（{latency:.1f}s）")

    def flush_stream(self) -> None:
        """收尾流式行。中断（Ctrl+C）等情况必须调用，否则后续输出会接在同一行。"""
        if self._stream_kind is not None:
            with self._lock:
                self._close_stream_line()

    # ---------------------------------------------------------- 工具轨迹

    def tools_start(self, turn: int, items: list) -> None:
        """一批工具即将执行。默认实现就是逐个打印，子类可以做得更花。"""
        for tool, args in items:
            self.tool_start(turn, tool, args)

    def tool_start(self, turn: int, tool, args: dict) -> None:
        preview = ", ".join(f"{k}={brief(v, 60)}" for k, v in args.items())
        self._p(f"\n[第 {turn} 轮] → 调用 {tool.name}({preview})")

    def tool_end(self, turn: int, tool, result: str, seconds: float) -> None:
        head = result.replace("\n", " ")
        self._p(f"         ← 返回 {len(result)} 字符: {head[:160]}")

    def tools_done(self, turn: int, count: int, wall: float, longest: float) -> None:
        """一个并行批次的收尾统计。串行耗时 ≈ count × 平均，并行 ≈ longest。"""
        saved = count * longest - wall if longest else 0.0
        speed = (count * longest / wall) if wall > 0 and longest else 0.0
        self._p(
            f"         ⏱ {count} 个工具并行完成，墙钟 {wall:.2f}s"
            f"（最长单个 {longest:.2f}s，约省 {saved:.2f}s，加速 {speed:.1f}x）"
        )

    # ---------------------------------------------------------- 取消通知

    def cancelled(self, reason: str = "") -> bool:
        """任务被取消时的一次性提示。返回 True 表示「已经显示过」。

        返回值不是装饰：repl / main 靠它避免同一件事被打印两遍
        （跟 llm_delta → last_answer_streamed 是同一套约定）。
        reason 来自取消令牌，回答的是「为什么停了」而不是「停了」——
        没有它，用户看到的中断提示永远是同一句没有信息量的话。
        """
        if not self.enabled:
            return False
        tail = f"：{reason}" if reason else ""
        self._p(f"\n⏹ 已取消{tail}")
        return True

    # ---------------------------------------------------------- 交互确认

    def ask_confirm(self, tool, args: dict) -> bool:
        preview = ", ".join(f"{k}={brief(v, 50)}" for k, v in args.items())
        with self._lock:
            sys.stderr.write(f"\n⚠  即将执行危险工具 {tool.name}({preview})\n确认执行？[y/N] ")
            sys.stderr.flush()
        try:
            return input().strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def ask_user(self, question: str, options: list, allow_free_text: bool = True,
                 multi: bool = False) -> str:
        """模型主动提问（ask_user）：打印问题与候选，回车跳过。"""
        opts = [str(o) for o in (options or [])]
        with self._lock:
            sys.stderr.write(f"\n❓ {question}\n")
            for i, o in enumerate(opts, 1):
                sys.stderr.write(f"   {i}) {o}\n")
            sys.stderr.write(f"{_ask_hint(allow_free_text, multi)}（回车跳过）：")
            sys.stderr.flush()
        try:
            ans = input().strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        return _resolve_answer(ans, opts, allow_free_text, multi)


class RichUI(PlainUI):
    """彩色实现。rich 缺失时自动退回 PlainUI。"""

    def __init__(self, enabled: bool = True, console=None) -> None:
        super().__init__(enabled)
        self.console = console

    # ---------------------------------------------------------- 基础输出

    def notice(self, text: str = "") -> None:
        if self.enabled and self.console:
            with self._lock:
                self.console.print(text)

    def turn_start(self, turn: int) -> None:
        pass

    def llm_done(self, turn: int, latency: float, streamed: bool = False) -> None:
        if self._stream_kind is not None:
            with self._lock:
                self._close_stream_line()
        if self.enabled and self.console:
            with self._lock:
                self.console.print(f"[dim]· 第 {turn} 轮 · 模型 {latency:.1f}s[/dim]")

    # ---------------------------------------------------------- 工具轨迹

    def tools_start(self, turn: int, items: list) -> None:
        if not (self.enabled and self.console) or len(items) < 2:
            return super().tools_start(turn, items)
        # 并行批次：先一次性列出这一轮要跑哪些工具，后面谁先完成谁先出结果
        preview = " + ".join(t.name for t, _a in items)
        with self._lock:
            self.console.print(
                f"  [cyan]⚙[/cyan] [bold]并行执行 {len(items)} 个工具[/bold][dim]（{preview}）[/dim]"
            )
            for t, args in items:
                p = ", ".join(f"{k}={brief(v, 40)}" for k, v in args.items())
                danger = "[red]危险[/red] " if t.dangerous else ""
                self.console.print(f"      {danger}[bold]{t.name}[/bold][dim]({p})[/dim]")

    def tool_start(self, turn: int, tool, args: dict) -> None:
        if not (self.enabled and self.console):
            return
        preview = ", ".join(f"{k}={brief(v, 60)}" for k, v in args.items())
        danger = "[red]危险[/red] " if tool.dangerous else ""
        with self._lock:
            self.console.print(f"  [cyan]⚙[/cyan] {danger}[bold]{tool.name}[/bold][dim]({preview})[/dim]")

    def tool_end(self, turn: int, tool, result: str, seconds: float) -> None:
        if not (self.enabled and self.console):
            return
        first = self._escape(result.replace("\n", " ")[:120])
        color = "yellow" if result.startswith("[error]") or "[blocked]" in result[:40] else "dim"
        with self._lock:
            self.console.print(
                f"     [dim]↳[/dim] [cyan]{tool.name}[/cyan] "
                f"[{color}]{len(result)} 字符 · {seconds * 1000:.0f}ms · {first}[/{color}]"
            )

    def tools_done(self, turn: int, count: int, wall: float, longest: float) -> None:
        if not (self.enabled and self.console):
            return
        if longest and wall > 0:
            speed = count * longest / wall
            note = f"[green]加速 ≈ {speed:.1f}x[/green]"
        else:
            note = ""
        with self._lock:
            self.console.print(
                f"     [dim]⏱ {count} 个工具并行完成，墙钟 {wall:.2f}s"
                f"（最长单个 {longest:.2f}s）[/dim] {note}"
            )

    # ---------------------------------------------------------- 取消通知

    def cancelled(self, reason: str = "") -> bool:
        if not (self.enabled and self.console):
            return super().cancelled(reason)
        tail = f"[dim]：{self._escape(reason)}[/dim]" if reason else ""
        with self._lock:
            self.console.print(f"\n[yellow]⏹ 已取消[/yellow]{tail}")
        return True

    # ---------------------------------------------------------- 交互确认

    def ask_confirm(self, tool, args: dict) -> bool:
        preview = ", ".join(f"{k}={brief(v, 50)}" for k, v in args.items())
        if not self.console:
            return super().ask_confirm(tool, args)
        with self._lock:
            self.console.print(
                f"\n[bold yellow]⚠ 需要确认[/bold yellow] [bold]{tool.name}[/bold][dim]({preview})[/dim]"
            )
            try:
                ans = self.console.input("[yellow]执行这个危险操作？[y/N] [/yellow]")
            except (EOFError, KeyboardInterrupt):
                return False
        return ans.strip().lower() in ("y", "yes")

    def ask_approval(self, reason: str) -> bool:
        """auto 档模型主动申请授权（request_approval）的终端询问。"""
        if not self.console:
            return False
        with self._lock:
            self.console.print(
                f"\n[bold cyan]🛡 模型请求授权[/bold cyan]\n{self._escape(reason)}"
            )
            try:
                ans = self.console.input("[cyan]批准这次操作？[y/N] [/cyan]")
            except (EOFError, KeyboardInterrupt):
                return False
        return ans.strip().lower() in ("y", "yes")

    def ask_user(self, question: str, options: list, allow_free_text: bool = True,
                 multi: bool = False) -> str:
        """模型主动提问（ask_user）的终端询问。"""
        if not self.console:
            return super().ask_user(question, options, allow_free_text, multi)
        opts = [str(o) for o in (options or [])]
        with self._lock:
            self.console.print(
                f"\n[bold cyan]❓ 模型提问[/bold cyan] {self._escape(question)}"
            )
            for i, o in enumerate(opts, 1):
                self.console.print(f"  [cyan]{i}[/cyan]) {self._escape(o)}")
            hint = _ask_hint(allow_free_text, multi)
            try:
                ans = self.console.input(f"[cyan]{hint}（回车跳过）：[/cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        return _resolve_answer(ans, opts, allow_free_text, multi)

    @staticmethod
    def _escape(text: str) -> str:
        try:
            from rich.markup import escape

            return escape(text or "")
        except Exception:  # noqa: BLE001
            return text or ""


def make_ui(prefer_rich: bool = True, enabled: bool = True):
    """工厂：优先返回 RichUI，rich 不可用时退回 PlainUI。"""
    if prefer_rich:
        try:
            from rich.console import Console

            return RichUI(enabled=enabled, console=Console())
        except Exception:  # noqa: BLE001 —— rich 没装或初始化失败都不该影响主流程
            pass
    return PlainUI(enabled=enabled)
