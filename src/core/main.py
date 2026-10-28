"""
mini-agent 入口。

用法：
    python main.py                          进入控制台（推荐）
    python main.py "帮我看一下 data.csv"      一次性执行一个任务
    python main.py --tools                  列出已注册工具（不需要 API Key）
    python main.py --plugins                列出插件模块与加载状态
    python main.py --mcp-status             检查 MCP server 连接
    python main.py --selftest               工具层自检
    python main.py --web                    启动 Web UI（浏览器聊天界面）

常用开关：
    --model NAME        覆盖模型名
    --workspace PATH    指定工作区（沙箱根目录）
    --data-dir PATH     数据目录（配置 / 会话 / 账号库 / 日志；默认 ~/.mini-agent）
    --plugin-dir A,B    替换插件目录
    --no-mcp            不挂载 MCP server
    --no-plugins        不加载插件（调试用）
    --no-stream         关闭流式输出（改用整段返回，配合富文本排版更整齐）
    --no-parallel       关闭并行工具执行（同一轮的多个工具改为串行）
    --workers N         并行工具线程数（默认 4）
    --approve           危险工具执行前需要人工确认
    --plain             不用彩色输出
    -q / --quiet        不显示工具调用轨迹

Web UI 开关（配合 --web）：
    --port N            监听端口（默认 8000）
    --host ADDR         监听地址（默认 0.0.0.0，本机 + 局域网均可访问）
                        传 127.0.0.1 可收窄为仅本机；启动时会打印局域网访问地址
    --no-login          关闭登录校验（仅调试/自动化测试；任何能访问端口的人都能操作本机）

账号：
    首次启动会自动创建 admin / admin123，登录后强制改密；
    --reset-password U  在终端重置某个账号的密码后退出（忘记密码的救援通道）

结构：main() 只做编排，真正的活儿在下面的分段函数里：
    _bootstrap        所有路径共用的前半场（环境变量 → UI → 插件 → 技能）
    _cmd_*            只读子命令（看完就走，不需要模型）
    _attach_mcp       挂载 MCP server
    _build_agent      拼 system prompt 并建 Agent
    _run_prompt_once  一次性任务模式
    _run_console      交互式控制台模式
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

# 无论以哪种方式启动（python -m core / 直接跑 main.py），
# 都先把 sys.path 铺好，再算根目录 —— 顺序不能反。
if __package__:
    from ._paths import ROOT as _ROOT
    from ._paths import install_import_paths
else:
    # 直接以脚本方式运行（python src/core/main.py）时没有包上下文，
    # 先把本文件所在目录放上 sys.path，再按裸模块名导入。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _paths import ROOT as _ROOT
    from _paths import install_import_paths

install_import_paths()

if TYPE_CHECKING:  # 仅为类型标注；运行期都是惰性导入
    from core.agent import Agent
    from core.mcp import MCPManager
    from core.skills import SkillLoader, SkillLoadReport

#: 项目根目录。mcp_servers.json、plugins/、demo_case/ 都在它下面。
#: 注意不是「本文件所在目录」（那是包内的 src/core/），
#: 所以这里**不能**再用 os.path.dirname(__file__)。
HERE = _ROOT


def load_dotenv(path: str | None = None) -> None:
    """极简 .env 读取，避免额外依赖。

    path 省略时依次找：当前目录的 .env → 项目根的 .env。
    后者保证「从任意目录启动」（README 承诺的用法）也能读到配置。
    """
    candidates = [path] if path else [".env", os.path.join(HERE, ".env")]
    for p in candidates:
        if p and os.path.isfile(p):
            break
    else:
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _safe_print(text: str) -> None:
    """打印最终回答。模型输出里什么都可能有，别让编码把整个程序带崩。"""
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
            print(text)
        except Exception:  # noqa: BLE001
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mini-agent",
        description="一个可扩展的本地智能体控制台：插件目录 + MCP 双通道扩展工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("prompt", nargs="*", help="一次性任务；不传则进入控制台")
    ap.add_argument("--tools", action="store_true", help="列出已注册工具后退出")
    ap.add_argument("--plugins", action="store_true", help="列出插件模块后退出")
    ap.add_argument("--skills", action="store_true", help="列出技能后退出")
    ap.add_argument("--mcp-status", action="store_true", help="检查 MCP server 后退出")
    ap.add_argument("--selftest", action="store_true", help="工具层自检后退出")
    ap.add_argument("--model", default=None, help="覆盖模型名")
    ap.add_argument("--workspace", default=None, help="工作区（沙箱根目录）")
    ap.add_argument(
        "--data-dir", default=None,
        help="数据目录（配置 / 会话 / 账号库 / 日志；默认 ~/.mini-agent）",
    )
    ap.add_argument("--plugin-dir", default=None, help="插件目录，逗号分隔（替换默认目录）")
    ap.add_argument("--skill-dir", default=None, help="技能目录，逗号分隔（替换默认目录）")
    ap.add_argument("--max-turns", type=int, default=None,
                    help="最大轮次（不填则读 Web UI 的通用配置，默认 25）")
    ap.add_argument("--no-mcp", action="store_true", help="不挂载 MCP server")
    ap.add_argument("--no-plugins", action="store_true", help="不加载插件")
    ap.add_argument("--no-skills", action="store_true", help="不加载技能")
    ap.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    ap.add_argument("--no-parallel", action="store_true", help="关闭并行工具执行")
    ap.add_argument("--workers", type=int, default=None, help="并行工具线程数（默认 4）")
    ap.add_argument("--approve", action="store_true", help="危险工具需人工确认")
    ap.add_argument("--plain", action="store_true", help="不使用彩色输出")
    ap.add_argument("-q", "--quiet", action="store_true", help="不显示工具调用轨迹")
    ap.add_argument("--web", action="store_true", help="启动 Web UI（浏览器聊天界面）")
    ap.add_argument("--no-login", action="store_true",
                    help="关闭登录校验（仅调试/自动化测试；任何能访问端口的人都能操作本机）")
    ap.add_argument("--reset-password", metavar="USERNAME", default=None,
                    help="重置某个本地账号的密码后退出（终端救援通道）")
    ap.add_argument("--port", type=int, default=8000, help="Web UI 监听端口（默认 8000）")
    ap.add_argument(
        "--host",
        default="0.0.0.0",
        help="Web UI 监听地址（默认 0.0.0.0，局域网可访问；传 127.0.0.1 收窄为仅本机）",
    )
    return ap


# ---------------------------------------------------------------- 装配

def _apply_cli_env(args: argparse.Namespace) -> None:
    """把命令行开关翻译成环境变量。

    必须在 import tools / agent 之前调用 —— 工作区与插件/技能目录
    都在模块导入时读取，顺序反了开关就不生效。
    """
    if args.workspace:
        os.environ["AGENT_WORKSPACE"] = os.path.abspath(args.workspace)
    if args.no_plugins:
        os.environ["AGENT_NO_PLUGINS"] = "1"
    elif args.plugin_dir:
        os.environ["AGENT_PLUGIN_DIRS"] = args.plugin_dir
    if args.no_skills:
        os.environ["AGENT_NO_SKILLS"] = "1"
    elif args.skill_dir:
        os.environ["AGENT_SKILL_DIRS"] = args.skill_dir


def _bootstrap(args: argparse.Namespace) -> tuple[Any, SkillLoader, SkillLoadReport]:
    """所有路径共用的前半场：环境变量 → UI → 插件 → 技能。

    返回 (ui, skill_loader, skill_report)。
    """
    load_dotenv()
    _apply_cli_env(args)

    # 默认工作区目录（当前登录用户的数据目录，如 ~/mini-agent）首次运行时
    # 可能还不存在，在这里先建好 —— Web 后端、控制台、--selftest 都要拿它
    # 当工具的工作目录（cwd / 沙箱根）。
    # 显式给了 --workspace 就跳过，免得白建一个用不到的目录。
    if not os.environ.get("AGENT_WORKSPACE"):
        from core import workspace_config

        workspace_config.ensure_default_workspace()

    from core.ui import make_ui

    ui = make_ui(prefer_rich=not args.plain, enabled=not args.quiet)

    import importlib

    importlib.import_module("tools")  # 导入即加载插件

    from core.registry import REGISTRY
    from core.skills import SkillLoader, register_use_skill_tool

    # 技能：发现/解析 skills、skills_user 下的 SKILL.md，并注册 use_skill 入口工具
    skill_loader = SkillLoader(HERE)
    skill_report = skill_loader.load_all()
    if skill_loader.dirs:
        register_use_skill_tool(REGISTRY, skill_loader)
    return ui, skill_loader, skill_report


# ---------------------------------------------------------------- 只读子命令

def _cmd_list_tools(selftest: bool) -> int:
    """--tools / --selftest：列出全部工具；selftest 额外跑一遍 list_dir。"""
    import tools

    from core.registry import REGISTRY
    from core.sandbox import workspace as ws

    for t in REGISTRY.all():
        flag = " [dangerous]" if t.dangerous else ""
        origin = f" ← {t.origin}" if t.origin else ""
        print(f"- {t.name}{flag}  [{t.group} / {t.source}]{origin}")
        print(f"    {t.description}")
    if not selftest:
        return 0
    print()
    print("工作区:", ws())
    print("工具数:", len(REGISTRY))
    print("插件加载:", tools.load_report().summary())
    print(REGISTRY.call("list_dir", {"path": "."}))
    return 0


def _cmd_list_plugins() -> int:
    """--plugins：列出插件模块与加载报告。"""
    import tools

    ldr = tools.loader()
    for m in ldr.loaded_modules():
        info = ldr.module_info(m)
        print(f"- {m}  ({len(info['tools'])} 个工具)  {info['doc']}")
        if info["info"]:
            print(f"    PLUGIN_INFO: {info['info']}")
    rep = tools.load_report()
    print()
    print("加载报告:", rep.summary())
    for m, err in rep.failed.items():
        print(f"  [失败] {m}: {err}")
    for name, why in rep.skipped.items():
        print(f"  [冲突] {name}: {why}")
    return 0


def _cmd_list_skills(loader: SkillLoader, report: SkillLoadReport) -> int:
    """--skills：列出技能清单。"""
    skills = loader.all()
    if not skills:
        print("（没有找到任何技能。往 skills/ 或 skills_user/ 放一个含 SKILL.md 的目录即可。）")
    else:
        for s in skills:
            print(f"- {s.name}: {s.description}")
            print(f"    {os.path.relpath(s.path, HERE)}")
    for name, err in report.failed.items():
        print(f"  [失败] {name}: {err}")
    return 0


def _cmd_mcp_status(manager: MCPManager) -> int:
    """--mcp-status：打印各 server 的连接状态。"""
    print(manager.report.summary())
    for name, st in manager.report.servers.items():
        print(f"- {name}: {st.status}  tools={st.tools}")
        if st.error:
            print(f"    错误：{st.error}")
    return 0


# ---------------------------------------------------------------- 运行路径

def _attach_mcp(args: argparse.Namespace) -> MCPManager | None:
    """按配置挂载 MCP server；--no-mcp 时返回 None。"""
    if args.no_mcp:
        return None
    from core.mcp import MCPManager
    from core.registry import REGISTRY

    manager = MCPManager(
        config_path=os.path.join(HERE, "mcp", "mcp_servers.json"),
        project_root=HERE,
        registry=REGISTRY,
    )
    manager.load()
    return manager


def _build_agent(
    args: argparse.Namespace,
    ui: Any,
    skill_loader: SkillLoader,
) -> tuple[Agent, str]:
    """拼 system prompt（基础 + 技能清单）并建 Agent。返回 (agent, 基础 prompt)。"""
    from core.agent import Agent, DEFAULT_SYSTEM_PROMPT
    from core.registry import REGISTRY

    skill_index = skill_loader.render_index()
    system_prompt = DEFAULT_SYSTEM_PROMPT
    if skill_index:
        system_prompt += "\n\n" + skill_index

    agent = Agent(
        model=args.model,
        system_prompt=system_prompt,
        max_turns=args.max_turns,
        verbose=not args.quiet,
        ui=ui,
        registry=REGISTRY,
        approve=args.approve,
        # 命令行显式关了才传 False；否则传 None，让通用配置决定
        stream=False if args.no_stream else None,
        parallel_tools=False if args.no_parallel else None,
        max_workers=args.workers,
        # 控制台交互共用一条长期会话记忆；一次性 -p 模式在运行前置空
        session_id="repl",
    )
    return agent, DEFAULT_SYSTEM_PROMPT


def _run_prompt_once(agent: Agent, prompt: str, mcp_manager: MCPManager | None) -> int:
    """一次性任务模式：跑完打印回答与统计。返回进程退出码。"""
    from core.cancel import Cancelled

    cancels_before = mcp_manager.cancel_stats()["cancellations"] if mcp_manager else 0
    # 一次性脚本模式不沉淀主题摘要，也不触发额外提炼调用
    agent.session_id = None
    try:
        answer = agent.run(prompt)
    except Cancelled as e:
        # 提示通常已由 UI 层打过（agent 会在取消收尾时通知 UI）；
        # 没打过才在这里补一句，避免同一件事说两遍。
        if not getattr(agent, "last_cancel_notice_shown", False):
            print(f"\n[cancelled] 已取消：{e}", file=sys.stderr)
        if mcp_manager:
            delta = mcp_manager.cancel_stats()["cancellations"] - cancels_before
            if delta:
                print(f"[cancelled] 已向 MCP server 补发 {delta} 条取消通知。", file=sys.stderr)
        return 130
    finally:
        if mcp_manager:
            mcp_manager.shutdown()
    # 流式模式下回答已经逐字打过了，再 print 一遍就是重复
    if not agent.last_answer_streamed:
        _safe_print(answer)
    u = agent.usage
    logging.getLogger("mini-agent.app").info(
        "prompt.done llm_calls=%d tool_calls=%d tokens=%d",
        u["llm_calls"], u["tool_calls"], u["total_tokens"],
    )
    print(
        f"\n[统计] LLM 调用 {u['llm_calls']} 次 | 工具调用 {u['tool_calls']} 次 | "
        f"tokens 输入 {u['prompt_tokens']} + 输出 {u['completion_tokens']} = {u['total_tokens']}",
        file=sys.stderr,
    )
    return 0


def _run_console(
    agent: Agent,
    ui: Any,
    mcp_manager: MCPManager | None,
    skill_loader: SkillLoader,
    system_base: str,
) -> int:
    """交互式控制台模式。"""
    import tools

    from core.repl import Console
    from core.sandbox import workspace as ws

    console = Console(
        agent=agent,
        loader=tools.loader(),
        mcp_manager=mcp_manager,
        ui=ui,
        model=agent.model,
        workspace=ws(),
        skill_loader=skill_loader,
        system_base=system_base,
    )
    try:
        return console.run()
    finally:
        if mcp_manager:
            mcp_manager.shutdown()


# ---------------------------------------------------------------- Web UI 模式

def _run_web(
    args: argparse.Namespace,
    skill_loader: SkillLoader,
    mcp_manager: MCPManager | None,
) -> int:
    """Web UI 模式：起 HTTP 服务阻塞运行，Ctrl+C 退出。"""
    from core.agent import DEFAULT_SYSTEM_PROMPT
    from core.webui import serve

    skill_index = skill_loader.render_index()
    system_prompt = DEFAULT_SYSTEM_PROMPT
    if skill_index:
        system_prompt += "\n\n" + skill_index

    try:
        serve(
            host=args.host,
            port=args.port,
            model=args.model or os.environ.get("OPENAI_MODEL"),
            system_prompt=system_prompt,
            max_turns=args.max_turns,
            login_enabled=not args.no_login,
            skill_loader=skill_loader,
        )
    except OSError as e:
        logging.getLogger("mini-agent.app").exception("Web UI 启动失败：%s", e)
        print(f"错误：Web UI 启动失败（{e}）。端口 {args.port} 可能已被占用。", file=sys.stderr)
        return 1
    finally:
        if mcp_manager:
            mcp_manager.shutdown()
    return 0


# ---------------------------------------------------------------- 数据目录

def _apply_data_dir(explicit: str | None) -> None:
    """确定可写数据目录，并在首次升级时迁移旧数据。

    顺序很重要：settings / model_config / webui 等模块的路径常量都在**导入时**
    计算，所以必须先写环境变量，再让那些模块被惰性导入。
    """
    from core._paths import DATA_DIR_ENV, data_dir, migrate_legacy_data

    if explicit:
        os.environ[DATA_DIR_ENV] = os.path.abspath(os.path.expanduser(explicit))
    moved = migrate_legacy_data()
    if moved:
        print(
            f"数据目录：{moved}（已从旧目录复制，原目录保留，确认无误后可自行删除）"
        )


# ---------------------------------------------------------------- 账号救援

def _cmd_reset_password(username: str) -> int:
    """终端救援通道：重置本地账号密码后退出。

    为什么不做成 HTTP 接口：那等于给系统留一个「无需登录即可改密」的后门。
    只有能登录这台机器、能执行命令的人，才应该拥有这个能力。
    """
    import getpass

    from core import auth

    auth.init_db()
    if auth.get_user_by_name(username) is None:
        print(f"错误：账号 {username} 不存在。", file=sys.stderr)
        return 1
    try:
        pwd = getpass.getpass("新密码：")
        confirm = getpass.getpass("再输一次：")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        return 1
    if pwd != confirm:
        print("错误：两次输入不一致。", file=sys.stderr)
        return 1
    try:
        auth.reset_password(username, pwd)
    except auth.AuthError as e:
        print(f"错误：{e.message}", file=sys.stderr)
        return 1
    print(f"已重置 {username} 的密码（下次登录后无需再强制改密）。")
    return 0


# ---------------------------------------------------------------- 编排

def main() -> int:
    args = build_parser().parse_args()

    # 数据目录必须最先确定：后续模块的路径常量都在导入时计算
    _apply_data_dir(args.data_dir)

    # 账号救援不依赖模型与插件：先处理，避免被「没配 Key」挡在门外
    if args.reset_password:
        return _cmd_reset_password(args.reset_password)

    ui, skill_loader, skill_report = _bootstrap(args)

    # 通用配置（Web UI「系统配置 → 通用配置」可改）：把运行时项先落地；
    # 其余参数（轮数/温度/流式/并行…）由 Agent 构造时按同一份配置读取
    from core import settings

    settings.apply_to_runtime()

    # 文件日志已由 apply_to_runtime → logging_setup.reconfigure() 按配置初始化
    from core import logging_setup
    from core.sandbox import workspace as _workspace

    _app_log = logging_setup.get_logger("app")
    _mode = "web" if args.web else ("prompt" if args.prompt else "console")
    _app_log.info(
        "启动 mode=%s workspace=%s model=%s host=%s port=%s",
        _mode, _workspace(), os.environ.get("OPENAI_MODEL") or "(默认)",
        args.host, args.port,
    )

    # ---- 只读子命令：看完就走，不需要模型 ----
    if args.tools or args.selftest:
        return _cmd_list_tools(selftest=args.selftest)
    if args.plugins:
        return _cmd_list_plugins()
    if args.skills:
        return _cmd_list_skills(skill_loader, skill_report)

    mcp_manager = _attach_mcp(args)
    if mcp_manager is not None and args.mcp_status:
        try:
            return _cmd_mcp_status(mcp_manager)
        finally:
            mcp_manager.shutdown()

    # ---- 以下需要模型 ----
    if not os.environ.get("OPENAI_API_KEY"):
        _app_log.warning("未设置 OPENAI_API_KEY，已中止启动")
        print(
            "错误：未设置 OPENAI_API_KEY。请复制 .env.example 为 .env 并填写，或直接导出环境变量。",
            file=sys.stderr,
        )
        if mcp_manager:
            mcp_manager.shutdown()
        return 2

    if args.web:
        return _run_web(args, skill_loader, mcp_manager)

    agent, system_base = _build_agent(args, ui, skill_loader)
    if args.prompt:
        return _run_prompt_once(agent, " ".join(args.prompt), mcp_manager)
    return _run_console(agent, ui, mcp_manager, skill_loader, system_base)


if __name__ == "__main__":
    raise SystemExit(main())
