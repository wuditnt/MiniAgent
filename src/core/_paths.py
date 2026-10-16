"""
路径锚点 —— 整个项目「根目录在哪」的唯一答案。

为什么需要这个文件？
    项目里几乎所有路径都是相对的（`demo_case/inbox/x.csv`、`mcp_servers.json`、
    插件目录 `plugins/`），而且有三个互相独立的东西需要知道「根」在哪：

      1. 运行时的包（找 mcp_servers.json、找插件目录、定沙箱根）
      2. 脚本层（setup_venv / run_tests 这些要 cd 到根）
      3. 测试层（把根放上 sys.path 才能 import 到东西）

    「根」的三种可能位置：
      · 从源码直接跑：<repo>/src/core/_paths.py            → 上溯 2 层
      · 可编辑安装后：同上（editable 装的是 .pth 指向源码目录）
      · 正常安装进 site-packages：包已被复制走，源码树不在了

    前两种都能靠 __file__ 上溯 2 层拿到 <repo>；第三种拿不到，所以这里必须
    有兜底：优先读环境变量 MINI_AGENT_ROOT，其次才靠 __file__ 推算。

为什么不用 importlib.resources？
    因为 `plugins/`、`plugins_user/`、`demo_case/` 是**用户要改、要放东西**的
    目录，不是包内只读资源。它们必须在仓库里、可写、能被 os.listdir 扫到。
    放进包内再用 resources API 读，反而把「约定优于配置」的设计搞坏了。
"""

from __future__ import annotations

import os

_ENV_VAR = "MINI_AGENT_ROOT"

#: 包自身所在目录 —— `src/core/`
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
#: 布局根 —— `src/`
SRC_DIR = os.path.dirname(PACKAGE_DIR)


def _detect_root() -> str:
    """推断项目根目录。顺序即优先级。"""
    # 1) 显式指定优先 —— 装进 site-packages 后这是唯一的正确来源
    explicit = os.environ.get(_ENV_VAR, "").strip()
    if explicit and os.path.isdir(explicit):
        return os.path.abspath(explicit)

    # 2) 源码布局：<root>/src/core/_paths.py → 上溯 2 层
    #    判断依据是「根目录下确实有项目标志物」（requirements.txt 或 mcp_servers.json）
    candidate = os.path.dirname(SRC_DIR)
    if any(
        os.path.isfile(os.path.join(candidate, mark))
        for mark in ("requirements.txt", "pyproject.toml", "mcp_servers.json")
    ):
        return candidate

    # 3) 退化：把 src/ 当成根（至少 sys.path 注入能工作）
    return candidate


#: 项目根目录（绝对路径）。仓库里的 plugins/、mcp_servers.json、demo_case/ 都在它下面。
ROOT = _detect_root()

#: 默认工作区（沙箱根）—— 落在**当前登录用户的数据目录**，而不是源码目录。
#: 为什么不能是 ROOT？ROOT 只是「放插件/配置/资源的地方」，而工作区是
#: 「智能体读写文件的地方」；两者混在一起，智能体就会把项目源码当工作区改。
#: 例：Windows → C:\Users\<you>\mini-agent；macOS/Linux → /home/<you>/mini-agent
#: 运行时若设了 AGENT_WORKSPACE（--workspace）则覆盖它。（测试也用该变量指回 ROOT）
_HOME = os.path.expanduser("~")
DEFAULT_WORKSPACE = (
    os.path.join(_HOME, "mini-agent") if _HOME and _HOME != "~" else ROOT
)


def install_import_paths() -> str:
    """把包目录与项目根放上 sys.path，并返回根目录。

    这是「裸模块名导入」能继续工作的唯一前提：

        from ui import PlainUI          # 裸名：也需要 PACKAGE_DIR(src/core) 在 sys.path
        import tools                    # 同上
        from core import tool           # 包前缀：需要 SRC_DIR(src) 在 sys.path
        from core.ui import PlainUI     # 同上

    一次性补两个位置，裸名写法与包前缀写法就都能跑。
    """
    roots = (PACKAGE_DIR, SRC_DIR, ROOT)
    for p in roots:
        if p and p not in os.sys.path:
            os.sys.path.insert(0, p)
    os.environ.setdefault(_ENV_VAR, ROOT)
    return ROOT
