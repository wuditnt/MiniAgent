"""
mini-agent 入口：启动 Web UI。

用法：
    python -m core        启动 Web UI（默认 0.0.0.0:8000）
    python src/core/main.py --web    等价（--web 保留给旧脚本）

启动开关：
    --port N            监听端口（默认读 MINI_AGENT_PORT，否则 8000）
    --host ADDR         监听地址（默认读 MINI_AGENT_HOST，否则 0.0.0.0）
                        传 127.0.0.1 可收窄为仅本机；启动时会打印局域网访问地址
    --model NAME        覆盖模型名
    --workspace PATH    指定工作区（沙箱根目录）
    --data-dir PATH     数据目录（配置 / 会话 / 账号库 / 日志；默认 ~/.mini-agent）
    --tool-dir A,B      替换工具目录
    --skill-dir A,B     替换技能目录
    --max-turns N       最大轮次（不填则读 Web UI 的通用配置，默认 10）
    --no-mcp            不挂载 MCP server
    --no-tools          不加载工具（调试用）
    --no-skills         不加载技能
    --no-login          关闭登录校验（仅调试/自动化测试；任何能访问端口的人都能操作本机）

账号：
    首次启动会自动创建 admin / admin123，登录后强制改密；
    --reset-password U  在终端重置某个账号的密码后退出（忘记密码的救援通道）

结构：main() 只做编排，真正的活儿在下面的分段函数里：
    _bootstrap        环境变量 → 工具 → 技能
    _attach_mcp       挂载 MCP server
    _run_web          起 HTTP 服务并阻塞运行
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING

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
    from core.mcp import MCPManager
    from core.skills import SkillLoader, SkillLoadReport

#: 项目根目录。mcp_servers.json、builtin_tools/、demo_case/ 都在它下面。
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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"错误：{name} 必须是整数，当前值：{raw!r}", file=sys.stderr)
        raise SystemExit(2) from None


def _env_port() -> int:
    port = _env_int("MINI_AGENT_PORT", 8000)
    if not 1 <= port <= 65535:
        print(f"错误：MINI_AGENT_PORT 必须在 1-65535 之间，当前值：{port}", file=sys.stderr)
        raise SystemExit(2)
    return port


def _env_host() -> str:
    return os.environ.get("MINI_AGENT_HOST", "").strip() or "0.0.0.0"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mini-agent",
        description="一个可扩展的本地智能体 Web 应用：工具目录 + 连接（MCP）双通道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 旧脚本用 `--web` 启动，保留为无操作开关
    ap.add_argument("--web", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--model", default=None, help="覆盖模型名")
    ap.add_argument("--workspace", default=None, help="工作区（沙箱根目录）")
    ap.add_argument(
        "--data-dir", default=None,
        help="数据目录（配置 / 会话 / 账号库 / 日志；默认 ~/.mini-agent）",
    )
    ap.add_argument("--tool-dir", default=None, help="工具目录，逗号分隔（替换默认目录）")
    ap.add_argument("--skill-dir", default=None, help="技能目录，逗号分隔（替换默认目录）")
    ap.add_argument("--max-turns", type=int, default=None,
                    help="最大轮次（不填则读 Web UI 的通用配置，默认 10）")
    ap.add_argument("--no-mcp", action="store_true", help="不挂载 MCP server")
    ap.add_argument("--no-tools", action="store_true", help="不加载工具")
    ap.add_argument("--no-skills", action="store_true", help="不加载技能")
    ap.add_argument("--no-login", action="store_true",
                    help="关闭登录校验（仅调试/自动化测试；任何能访问端口的人都能操作本机）")
    ap.add_argument("--reset-password", metavar="USERNAME", default=None,
                    help="重置某个本地账号的密码后退出（终端救援通道）")
    ap.add_argument("--port", type=int, default=_env_port(),
                    help="Web UI 监听端口（默认读 MINI_AGENT_PORT，否则 8000）")
    ap.add_argument(
        "--host",
        default=_env_host(),
        help="Web UI 监听地址（默认读 MINI_AGENT_HOST，否则 0.0.0.0；传 127.0.0.1 收窄为仅本机）",
    )
    return ap


# ---------------------------------------------------------------- 装配

def _apply_cli_env(args: argparse.Namespace) -> None:
    """把命令行开关翻译成环境变量。

    必须在 import tools / agent 之前调用 —— 工作区与工具/技能目录
    都在模块导入时读取，顺序反了开关就不生效。
    """
    if args.workspace:
        os.environ["MINI_AGENT_WORKSPACE"] = os.path.abspath(args.workspace)
    if args.no_tools:
        os.environ["MINI_AGENT_NO_TOOLS"] = "1"
    elif args.tool_dir:
        os.environ["MINI_AGENT_TOOL_DIRS"] = args.tool_dir
    if args.no_skills:
        os.environ["MINI_AGENT_NO_SKILLS"] = "1"
    elif args.skill_dir:
        os.environ["MINI_AGENT_SKILL_DIRS"] = args.skill_dir


def _bootstrap(args: argparse.Namespace) -> tuple[SkillLoader, SkillLoadReport]:
    """Web UI 启动的前半场：环境变量 → 工具 → 技能。

    返回 (skill_loader, skill_report)。
    """
    load_dotenv()
    _apply_cli_env(args)

    # 默认工作区目录（当前登录用户的数据目录，如 ~/mini-agent）首次运行时
    # 可能还不存在，在这里先建好 —— Web 后端要拿它当工具的工作目录
    # （cwd / 沙箱根）。显式给了 --workspace 就跳过，免得白建一个用不到的目录。
    if not os.environ.get("MINI_AGENT_WORKSPACE"):
        from core import workspace_config

        workspace_config.ensure_default_workspace()

    import importlib

    importlib.import_module("tools")  # 导入即加载工具

    from core.registry import REGISTRY
    from core.skills import SkillLoader, register_use_skill_tool

    # 技能：发现/解析 skills、skills_user 下的 SKILL.md，并注册 use_skill 入口工具
    skill_loader = SkillLoader(HERE)
    skill_report = skill_loader.load_all()
    if skill_loader.dirs:
        register_use_skill_tool(REGISTRY, skill_loader)
    return skill_loader, skill_report


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


# ---------------------------------------------------------------- Web UI 模式

def _run_web(
    args: argparse.Namespace,
    skill_loader: SkillLoader,
    mcp_manager: MCPManager | None,
) -> int:
    """起 HTTP 服务阻塞运行，Ctrl+C 退出。"""
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
            model=args.model,
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
    from core._paths import DATA_DIR_ENV, migrate_legacy_data

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

def _has_model_config() -> bool:
    """是否已有可用模型配置（models.json）—— 模型已改由 Web UI 配置，不看环境变量。

    只认**用户保存过**的记录：models.json 不存在时，配置层会按模板兜底造一条
    DeepSeek 记录（有地址、有模型名、但没有 Key），那不是「已配置」。
    需要 Key 的服务商还要求 Key 非空；Ollama / vLLM 这类本地服务允许无 Key。
    """
    try:
        from core import model_config

        if not os.path.exists(model_config._MODELS_FILE):
            return False
        entry = model_config.active_entry()
        if not entry:
            return False
        if not (entry.get("base_url") and entry.get("model")):
            return False
        tpl = model_config.PROVIDER_TEMPLATES.get(entry.get("provider") or "", {})
        return bool(entry.get("api_key")) or bool(tpl.get("no_key"))
    except Exception:  # noqa: BLE001 —— 配置层异常按「没配置」处理，给出可读提示
        return False


def main() -> int:
    # .env 必须先读：--host / --port 的默认值来自它
    load_dotenv()
    args = build_parser().parse_args()

    # 数据目录必须最先确定：后续模块的路径常量都在导入时计算
    _apply_data_dir(args.data_dir)

    # 账号救援不依赖模型与工具：先处理，避免被「没配 Key」挡在门外
    if args.reset_password:
        return _cmd_reset_password(args.reset_password)

    skill_loader, _skill_report = _bootstrap(args)

    # 通用配置（Web UI「系统配置 → 通用配置」可改）：把运行时项先落地；
    # 其余参数（轮数/温度/流式/并行…）由 Agent 构造时按同一份配置读取
    from core import settings

    settings.apply_to_runtime()

    # 文件日志已由 apply_to_runtime → logging_setup.reconfigure() 按配置初始化
    from core import logging_setup
    from core.sandbox import workspace as _workspace

    _app_log = logging_setup.get_logger("app")
    _app_log.info(
        "启动 workspace=%s model=%s host=%s port=%s",
        _workspace(), args.model or "(默认)", args.host, args.port,
    )

    mcp_manager = _attach_mcp(args)

    # 模型就在 Web UI 里配置：无配置也允许启动，进界面填好即可
    if not _has_model_config():
        _app_log.warning("尚未配置模型，启动后请在 Web UI 的「设置 → 模型」里配置")
        print(
            "提示：尚未配置模型。启动后在浏览器「设置 → 模型」里填写即可。",
            file=sys.stderr,
        )

    return _run_web(args, skill_loader, mcp_manager)


if __name__ == "__main__":
    raise SystemExit(main())
