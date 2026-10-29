"""
mini-agent 控制台。

它承担三件事：
  1. 交互式对话（多轮，会话上下文一直保留）
  2. 斜杠命令：不离开对话就能查看/管理工具与插件
  3. 工具调用的实时可视化（哪一轮、调了什么、花了多久、返回多大）

为什么斜杠命令很重要？
    可扩展 Agent 的瓶颈往往不是"写工具"，而是"改完工具要重启才能验证"。
    /reload 把这个循环从「改 → 退出 → 重启 → 重连 → 复现」压缩成「改 → /reload」。
"""

from __future__ import annotations

import re
import sys

from core.cancel import Cancelled
from core.registry import REGISTRY

_MARKUP_RE = re.compile(r"\[/?[a-z0-9 _#]+\]")


def _strip_markup(text: str) -> str:
    """剥掉 rich 标记 —— 没有 rich 的纯文本场景下，标记只会变成乱码。"""
    return _MARKUP_RE.sub("", text)

HELP_TEXT = """\
[bold]可用命令[/bold]
  [cyan]/help[/cyan]              显示本帮助
  [cyan]/tools[/cyan] [关键词]     列出工具（可按名字/描述过滤）
  [cyan]/plugins[/cyan]           列出插件模块与加载状态
  [cyan]/reload[/cyan] [模块名]    热重载插件（不传则全量重载）
  [cyan]/skills[/cyan] (reload)   列出技能 / 重新扫描技能目录
  [cyan]/skill[/cyan] [名字]       查看某个技能的完整指引
  [cyan]/mcp[/cyan] (reload)      查看 MCP server 状态 / 重新挂载
  [cyan]/model[/cyan] [名字]       查看或切换模型
  [cyan]/stream[/cyan] on|off     流式输出（逐字生成，感知更快）
  [cyan]/parallel[/cyan] on|off   同一轮的多个工具是否并行执行
  [cyan]/workers[/cyan] [N]       并行工具的最大线程数
  [cyan]/usage[/cyan]             本次会话的 token 与调用统计
  [cyan]/context[/cyan]           当前上下文占用
  [cyan]/verbose[/cyan] on|off    是否显示工具调用轨迹
  [cyan]/approve[/cyan] on|off    危险工具是否要人工确认
  [cyan]/remember[/cyan] [user|project] 内容   手动写入一条长期记忆（默认 project）
  [cyan]/memory[/cyan]            查看当前生效的用户画像 / 项目记忆 / 主题摘要
  [cyan]/reset[/cyan]             清空对话历史（保留 system 提示）
  [cyan]/exit[/cyan]              退出

[dim]直接输入自然语言即可下达任务；任务执行中按 Ctrl+C 立即取消
（正在运行的命令会被终止、对话历史保持一致），Ctrl+D 退出。[/dim]"""


class Console:
    def __init__(
        self,
        agent,
        loader=None,
        mcp_manager=None,
        ui=None,
        model: str = "",
        workspace: str = "",
        skill_loader=None,
        system_base=None,
    ) -> None:
        self.agent = agent
        self.loader = loader
        self.mcp = mcp_manager
        self.ui = ui
        self.model = model or getattr(agent, "model", "?")
        self.workspace = workspace
        self.skills = skill_loader          # SkillLoader 或 None（未启用）
        self._system_base = system_base      # 不含技能清单的原始 system prompt
        self._session = self._make_prompt_session()

    # ------------------------------------------------------------ 输入

    def _make_prompt_session(self):
        """有 prompt_toolkit 就用它（有历史、有补全），没有就退回 input()。

        仅在真实终端里启用 —— 管道/重定向场景下 prompt_toolkit 无法工作，
        退回 input() 才能让 `echo "/tools" | python main.py` 这类用法成立。
        """
        if not sys.stdin.isatty():
            return None
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import InMemoryHistory

            commands = [
                "/help", "/tools", "/plugins", "/reload", "/skills", "/skill",
                "/mcp", "/model", "/stream", "/parallel", "/workers", "/usage",
                "/context", "/verbose", "/approve", "/remember", "/memory",
                "/reset", "/exit",
            ]
            return PromptSession(
                history=InMemoryHistory(),
                completer=WordCompleter(commands, sentence=True),
            )
        except Exception:  # noqa: BLE001
            return None

    def _read(self, prompt: str) -> str:
        if self._session is not None:
            return self._session.prompt(prompt)
        # 纯文本模式：把 rich 标记剥掉，否则会看到 [bold green]>>> [/bold green]
        return input(_strip_markup(prompt))

    # ------------------------------------------------------------ 输出助手

    def _c(self):
        return getattr(self.ui, "console", None)

    def _print(self, text: str = "") -> None:
        console = self._c()
        if console:
            console.print(text)
        else:
            # 没有 rich：把最简单的标记去掉
            print(_strip_markup(text))

    def _print_answer(self, text: str) -> None:
        """最终回答按原样输出。

        不能用 _print：rich 会把模型输出里的 [xxx] 当标记解析，
        而模型回答里出现方括号的概率高得离谱（数组、注解、引用）。
        """
        console = self._c()
        if console:
            try:
                console.print(text, markup=False, highlight=False)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            print(text)
        except UnicodeEncodeError:
            try:
                sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
                print(text)
            except Exception:  # noqa: BLE001
                pass

    def _table(self, title: str, columns: list[str]):
        from rich.table import Table

        t = Table(title=title, show_lines=False, header_style="bold cyan")
        for col in columns:
            t.add_column(col)
        return t

    @staticmethod
    def _escape(text: str) -> str:
        try:
            from rich.markup import escape

            return escape(text or "")
        except Exception:  # noqa: BLE001
            return text or ""

    # ------------------------------------------------------------ 主循环

    def banner(self) -> None:
        groups = REGISTRY.groups()
        sources = REGISTRY.sources()
        line = " · ".join(f"{k} {v}" for k, v in sorted(sources.items()))
        self._print()
        self._print("[bold cyan]mini-agent[/bold cyan] [dim]— 可扩展的本地智能体控制台[/dim]")
        self._print(f"  [dim]模型[/dim] {self.model}    [dim]工作区[/dim] {self.workspace}")
        self._print(
            f"  [dim]工具[/dim] {len(REGISTRY)} 个（{line}）    "
            f"[dim]分组[/dim] {', '.join(sorted(groups)) or '—'}"
        )
        if self.mcp is not None:
            self._print(f"  [dim]MCP[/dim] {self.mcp.report.summary()}")
        if self.skills is not None:
            self._print(f"  [dim]技能[/dim] {len(self.skills.names())} 个")
        self._print(
            f"  [dim]流式[/dim] {'开' if self.agent.stream else '关'}    "
            f"[dim]并行工具[/dim] {'开' if self.agent.parallel_tools else '关'}"
            f"（最多 {self.agent.max_workers} 线程）"
        )
        self._print("  [dim]输入 /help 查看命令；直接说话即可下达任务[/dim]")
        self._print()

    def run(self) -> int:
        self.banner()
        while True:
            try:
                line = self._read("[bold green]>>> [/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                self._print("\n[dim]再见。[/dim]")
                return 0
            if not line:
                continue
            if line.startswith("/"):
                try:
                    keep = self.dispatch(line)
                except Exception as e:  # noqa: BLE001 —— 单条命令失败不该结束整个 REPL
                    self._print(
                        f"[red]命令执行失败：{type(e).__name__}[/red] "
                        f"[dim]{self._escape(str(e))}[/dim]"
                    )
                    continue
                if not keep:
                    self._print("[dim]再见。[/dim]")
                    return 0
                continue

            # 取消通知计数前先取快照：跑完再取一次就是「本轮真的补发了几条」
            mcp_cancels_before = self._mcp_cancels()
            try:
                answer = self.agent.run(line)
            except Cancelled:
                self._flush_stream()
                # 提示可能已经由 UI 层打过（agent 在取消收尾时统一通知 UI），
                # 这里只补一句 UI 不会说的事：本轮补发了几条取消通知。
                if not getattr(self.agent, "last_cancel_notice_shown", False):
                    self._print(
                        "\n[yellow]⏹ 已取消[/yellow]"
                        "[dim]（正在运行的命令已终止，对话历史保持一致，可直接继续）[/dim]"
                    )
                delta = self._mcp_cancels() - mcp_cancels_before
                if delta:
                    self._print(
                        f"[dim]  已向 MCP server 补发 {delta} 条取消通知"
                        f"（对端不会再把已放弃的请求跑到底）[/dim]"
                    )
                continue
            except KeyboardInterrupt:
                # 走到这里说明 Ctrl+C 落在收尾阶段（而不是任务里）—— 兜底再掐一次
                self._flush_stream()
                try:
                    self.agent.interrupt("用户按下 Ctrl+C（收尾阶段）")
                except Exception:  # noqa: BLE001
                    pass
                self._print("\n[yellow]已中断本轮。[/yellow]")
                continue
            except Exception as e:  # noqa: BLE001
                self._flush_stream()
                self._print(f"[red]调用失败：{type(e).__name__}: {e}[/red]")
                continue

            # 流式模式下这段文字已经边生成边打印过了，再 print 一次就会重复。
            # Agent 用 last_answer_streamed 告诉我们它到底打没打过。
            if not getattr(self.agent, "last_answer_streamed", False):
                self._print()
                self._print_answer(answer)
            u = self.agent.usage
            self._print(
                f"\n[dim]· 累计 LLM {u['llm_calls']} 次 | 工具 {u['tool_calls']} 次 | "
                f"tokens {u['total_tokens']}[/dim]\n"
            )

    def _flush_stream(self) -> None:
        fn = getattr(self.ui, "flush_stream", None)
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def _mcp_cancels(self) -> int:
        """累计已发出的 MCP 取消通知条数（没挂 MCP 时恒为 0）。"""
        if self.mcp is None:
            return 0
        try:
            return int(self.mcp.cancel_stats().get("cancellations", 0))
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------ 斜杠命令

    def dispatch(self, line: str) -> bool:
        """返回 False 表示退出。"""
        parts = line.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/exit", "/quit", "/q"):
            return False
        if cmd == "/help":
            self._print(HELP_TEXT)
        elif cmd == "/tools":
            self.cmd_tools(arg)
        elif cmd == "/plugins":
            self.cmd_plugins()
        elif cmd == "/reload":
            self.cmd_reload(arg)
        elif cmd == "/skills":
            self.cmd_skills(arg)
        elif cmd == "/skill":
            self.cmd_skill(arg)
        elif cmd == "/mcp":
            self.cmd_mcp(arg)
        elif cmd == "/model":
            self.cmd_model(arg)
        elif cmd == "/stream":
            self.cmd_toggle("stream", arg, label="流式输出")
        elif cmd == "/parallel":
            self.cmd_toggle("parallel_tools", arg, label="并行工具")
        elif cmd == "/workers":
            self.cmd_workers(arg)
        elif cmd == "/usage":
            self.cmd_usage()
        elif cmd == "/context":
            self.cmd_context()
        elif cmd == "/verbose":
            self.cmd_toggle("verbose", arg)
        elif cmd == "/approve":
            self.cmd_approval(arg)
        elif cmd == "/remember":
            # 内容是任意长文本，不能用 split 后的单参数
            remainder = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            self.cmd_remember(remainder)
        elif cmd == "/memory":
            self.cmd_memory()
        elif cmd == "/reset":
            self.agent.reset()
            self._print("[green]已清空对话历史。[/green]")
        else:
            self._print(f"[red]未知命令：{cmd}[/red] 输入 /help 查看全部命令")
        return True

    # ------------------------------------------------------------ 各命令实现

    def cmd_tools(self, keyword: str = "") -> None:
        tools = REGISTRY.find(keyword) if keyword else REGISTRY.all()
        if not tools:
            self._print(f"[yellow]没有匹配的工具：{keyword}[/yellow]")
            return
        title = f"已注册工具（{len(tools)}）" + (f" — 过滤「{keyword}」" if keyword else "")
        try:
            table = self._table(title, ["工具", "分组", "来源", "危险", "描述"])
            for t in tools:
                table.add_row(
                    self._escape(t.name),
                    self._escape(t.group),
                    "MCP" if t.source == "mcp" else t.source,
                    "[red]是[/red]" if t.dangerous else "",
                    self._escape(t.description[:70]),
                )
            self._c().print(table)
        except Exception:  # noqa: BLE001 —— 没有 rich 时降级
            for t in tools:
                flag = " [危险]" if t.dangerous else ""
                print(f"  {t.name}{flag}  [{t.group}/{t.source}]  {t.description}")

    def cmd_plugins(self) -> None:
        if self.loader is None:
            self._print("[dim]未启用插件加载器。[/dim]")
            return
        mods = self.loader.loaded_modules()
        if not mods:
            self._print("[yellow]没有加载任何插件。[/yellow]")
            return
        try:
            table = self._table(f"插件模块（{len(mods)}）", ["模块", "工具数", "工具"])
            for m in mods:
                tools = [t.name for t in REGISTRY.all() if t.module == m]
                table.add_row(self._escape(m), str(len(tools)), self._escape(", ".join(tools)))
            self._c().print(table)
        except Exception:  # noqa: BLE001
            for m in mods:
                tools = [t.name for t in REGISTRY.all() if t.module == m]
                print(f"  {m}  ({len(tools)}): {', '.join(tools)}")

    def cmd_reload(self, module: str = "") -> None:
        if self.loader is None:
            self._print("[dim]未启用插件加载器。[/dim]")
            return
        rep = self.loader.reload(module or None)
        if rep.reloaded:
            self._print(f"[green]已重载：{', '.join(rep.reloaded)}[/green]")
        if rep.loaded:
            self._print(f"[green]新加载：{', '.join(rep.loaded)}[/green]")
        if rep.removed:
            self._print(f"[yellow]已移除：{', '.join(rep.removed)}[/yellow]")
        for m, err in rep.failed.items():
            self._print(f"[red]失败 {m}：{err}[/red]")
        for name, why in rep.skipped.items():
            self._print(f"[yellow]跳过 {name}：{why}[/yellow]")
        self._print(
            f"[dim]现在共 {len(REGISTRY)} 个工具。"
            f"新加的工具会在下一轮对话自动出现在模型的工具列表里。[/dim]"
        )

    def cmd_skills(self, arg: str = "") -> None:
        if self.skills is None:
            self._print("[dim]未启用技能。[/dim]")
            return
        if arg.lower() == "reload":
            rep = self.skills.reload()
            self._refresh_skill_index()
            self._print(f"[green]已重新扫描技能目录：{rep.summary()}[/green]")
            for name, err in rep.failed.items():
                self._print(f"[red]失败 {name}：{err}[/red]")
        skills = self.skills.all()
        if not skills:
            self._print(
                "[dim]没有找到任何技能。往 skills/ 或 skills_user/ "
                "放一个含 SKILL.md 的目录即可。[/dim]"
            )
            return
        for s in skills:
            self._print(f"  [bold]{s.name}[/bold]  [dim]{s.description}[/dim]")
        self._print("[dim]用 /skill <名字> 查看完整指引；/skills reload 重新扫描。[/dim]")

    def cmd_skill(self, name: str = "") -> None:
        if self.skills is None:
            self._print("[dim]未启用技能。[/dim]")
            return
        if not name:
            self._print("[red]用法：/skill <名字>[/red]")
            return
        s = self.skills.get(name)
        if s is None:
            available = ", ".join(self.skills.names()) or "（无）"
            self._print(f"[red]未知技能：{name}[/red] 可用：{available}")
            return
        self._print(f"[bold cyan]{s.name}[/bold cyan]  [dim]{s.description}[/dim]")
        self._print_answer(s.body)

    def _refresh_skill_index(self) -> None:
        """热重载后把新的技能清单写回 system prompt，下一轮模型就能看到。"""
        if self.skills is None or self._system_base is None:
            return
        index = self.skills.render_index()
        self.agent.system_prompt = self._system_base + ("\n\n" + index if index else "")
        if self.agent.messages and self.agent.messages[0].get("role") == "system":
            self.agent.messages[0]["content"] = self.agent.system_prompt

    def cmd_mcp(self, arg: str = "") -> None:
        if self.mcp is None:
            self._print("[dim]未启用 MCP。[/dim]")
            return
        if arg.lower() == "reload":
            self.mcp.load()
            self._print("[green]已重新挂载 MCP server。[/green]")
        rep = self.mcp.report
        if not rep.servers:
            self._print("[dim]mcp_servers.json 里没有配置任何 server。[/dim]")
            return
        try:
            table = self._table("MCP server", ["名称", "状态", "工具", "说明"])
            for name, st in rep.servers.items():
                note = st.error or (
                    f"{st.server_info.get('name', '')} {st.server_info.get('version', '')}".strip()
                )
                color = {"ready": "green", "failed": "red"}.get(st.status, "yellow")
                table.add_row(
                    self._escape(name),
                    f"[{color}]{st.status}[/{color}]",
                    str(len(st.tools)),
                    self._escape(note)[:70],
                )
            self._c().print(table)
        except Exception:  # noqa: BLE001
            for name, st in rep.servers.items():
                print(f"  {name}: {st.status}  tools={len(st.tools)}  {st.error}")

        # 取消通知的可观测性：客户端侧只能证明「我不等了」，
        # 这两个数字才说明「通知确实发了 / 确实有对端没来得及收手」。
        cs = self.mcp.cancel_stats()
        if cs["requests"]:
            self._print(
                f"[dim]  协议调用 {cs['requests']} 次 | 取消通知 {cs['cancellations']} 条 | "
                f"超时 {cs['timeouts']} 次 | 丢弃迟到响应 {cs['late_responses']} 条[/dim]"
            )

    def cmd_model(self, name: str = "") -> None:
        if name:
            self.agent.model = name
            self.model = name
            self._print(f"[green]已切换模型：{name}[/green]")
        else:
            self._print(f"当前模型：[bold]{self.agent.model}[/bold]")

    def cmd_usage(self) -> None:
        u = self.agent.usage
        self._print(
            f"LLM 调用 [bold]{u['llm_calls']}[/bold] 次 | "
            f"工具调用 [bold]{u['tool_calls']}[/bold] 次\n"
            f"tokens 输入 {u['prompt_tokens']} + 输出 {u['completion_tokens']} "
            f"= [bold]{u['total_tokens']}[/bold]"
        )

    def cmd_context(self) -> None:
        s = self.agent.context_stats()
        pct = s["chars"] / s["limit"] * 100 if s["limit"] else 0
        self._print(
            f"消息 [bold]{s['messages']}[/bold] 条 | 约 [bold]{s['chars']}[/bold] 字符 "
            f"（上限 {s['limit']}，占用 {pct:.1f}%）| 已裁剪 {s['trimmed']} 条"
        )

    def cmd_approval(self, arg: str = "") -> None:
        """/approve on|off：on=按需询问（ask），off=完全控制（full）。

        三档模型下旧布尔开关映射到 ask/full；auto（自主判断）目前只在
        Web UI 提供切换。
        """
        cur = self.agent.approval_mode
        if arg.lower() == "on":
            value, mode = True, "ask"
        elif arg.lower() == "off":
            value, mode = False, "full"
        elif arg == "":
            value, mode = (False, "full") if cur == "ask" else (True, "ask")
        else:
            self._print("[red]用法：/approve on|off[/red]")
            return
        self.agent.approve = value
        self.agent.approval_mode = mode
        label = {"ask": "按需询问", "full": "完全控制", "auto": "自主判断"}[mode]
        self._print(f"[green]确认模式 = {label}（{mode}）[/green]")

    def cmd_remember(self, text: str) -> None:
        """/remember [user|project] 内容：手动写入长期记忆（默认 project 层）。"""
        from core import memory

        text = text.strip()
        if not text:
            self._print("[red]用法：/remember [user|project] 要记住的内容[/red]")
            return
        scope = "project"
        head = text.split(None, 1)[0].lower()
        if head in ("user", "project") and len(text.split(None, 1)) > 1:
            scope = head
            text = text.split(None, 1)[1].strip()
        result = memory.remember(scope, text)
        # remember() 的结果以 [ok]/[error]/[skipped] 开头，正文也可能带方括号：
        # 不转义的话 rich 会把它当颜色标记吞掉，用户看到的是「没有标签的句子」。
        self._print(f"[green]{self._escape(result)}[/green]")

    def cmd_memory(self) -> None:
        """展示当前工作区下三层记忆的位置与内容概览。"""
        from core import memory
        from core.sandbox import workspace

        ws = workspace()
        up = memory.load_user_profile().strip()
        pj = memory.load_project(ws).strip()
        topics = memory.list_topics(ws)
        self._print(f"[bold]工作区[/bold]：{ws}")
        self._print(f"[bold]用户画像[/bold]：{memory.user_profile_path()}")
        # 记忆正文是用户/模型写的自由文本，里面出现 [xxx] 的概率很高：
        # 原样交给 rich 会被当成标记解析（内容直接消失），必须先转义。
        self._print(self._escape(up) if up else "[dim]（空）[/dim]")
        self._print(f"\n[bold]项目记忆[/bold]：{memory.project_path(ws)}")
        self._print(self._escape(pj) if pj else "[dim]（空）[/dim]")
        self._print(f"\n[bold]主题摘要[/bold]：{len(topics)} 条（保留最近 {memory.TOPICS_KEEP} 条）")
        for it in topics[:10]:
            self._print(f"  · {it['session']}  ({it['size']} B)")
        if len(topics) > 10:
            self._print(f"  … 另有 {len(topics) - 10} 条")

    def cmd_toggle(self, attr: str, arg: str, label: str = "") -> None:
        if arg.lower() in ("on", "off"):
            value = arg.lower() == "on"
        elif arg == "":
            value = not getattr(self.agent, attr)
        else:
            self._print(f"[red]用法：/{attr} on|off[/red]")
            return

        setattr(self.agent, attr, value)
        if attr == "verbose":
            self.agent.ui.enabled = value
        self._print(f"[green]{label or attr} = {value}[/green]")

    def cmd_workers(self, arg: str = "") -> None:
        if not arg:
            self._print(
                f"并行工具线程数：[bold]{self.agent.max_workers}[/bold]"
                f"（当前并行{'开' if self.agent.parallel_tools else '关'}）"
            )
            return
        try:
            n = int(arg)
        except ValueError:
            self._print("[red]用法：/workers 4[/red]")
            return
        if n < 1:
            self._print("[red]至少为 1[/red]")
            return
        self.agent.max_workers = n
        self._print(f"[green]并行工具线程数 = {n}[/green]")
