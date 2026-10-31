"""
向后兼容层。

历史版本的 mini-agent 把所有工具都写在本文件里，现在工具已经拆成了独立模块
（见 builtin_tools/ 与 user_tools/）。本文件保留下来，
让老的写法继续可用：

    import tools
    tools.call("read_file", {"path": "x.txt"})
    tools.schemas()

新代码建议直接用：

    from core import REGISTRY, tool

包里其余模块用 `import tools` 这种裸模块名，靠的是 core/__init__.py
里的 sys.path 铺设（见 _paths.install_import_paths），不是靠本文件的相对导入。
"""

from __future__ import annotations

import os

from core._paths import ROOT

#: 工具目录（builtin_tools/、user_tools/）挂在项目根下，不是包内。
#: 旧代码里这是 os.path.dirname(__file__) —— 包化之后两者不再相等，
#: 用错了会导致「一个工具都没加载」还找不到原因。
PROJECT_ROOT = ROOT

from core.registry import (  # noqa: E402,F401
    REGISTRY,
    Tool,
    ToolRegistry,
    call,
    names,
    schemas,
    tool,
)
from core.sandbox import (  # noqa: E402,F401
    ALLOW_DANGEROUS,
    MAX_OUTPUT_CHARS,
    WORKSPACE,
    SandboxError,
    check_command,
    clip,
    decode_bytes,
    hexdump,
    looks_binary,
    read_bytes,
    safe_path,
    set_workspace,
    workspace,
)
from core.loader import ToolLoader  # noqa: E402

# 兼容旧的私有名
_clip = clip
_safe_path = safe_path

# 导入即加载工具 —— 保持 `import tools` 就能拿到全部工具的旧行为
_LOADER = ToolLoader(PROJECT_ROOT)
_REPORT = _LOADER.load_all()

# 核心工具：三档确认模式之「自主判断」用的授权申请（幂等，不受工具加载时序影响）
from core.approval import register as _register_approval  # noqa: E402
from core.ask import register as _register_ask  # noqa: E402
from core.memory import register as _register_memory  # noqa: E402
from core.subagent import register as _register_subagent  # noqa: E402
from core.tool_search import register as _register_tool_search  # noqa: E402

_register_approval()
_register_ask()
_register_memory()
_register_subagent()
_register_tool_search()


def loader() -> ToolLoader:
    """拿到工具加载器（加载报告与重载都走它）。"""
    return _LOADER


def load_report():
    """最近一次工具加载报告。"""
    return _REPORT


if __name__ == "__main__":
    print("工作区:", workspace())
    print("已注册工具:", ", ".join(names()))
    print("加载报告:", _REPORT.summary())
    print("--- 试跑 list_dir ---")
    print(call("list_dir", {"path": "."}))
