"""
core —— mini-agent 唯一的包：应用层与核心库都平铺在这里。

    ┌─ 应用层（入口与循环）──────────────────────────────┐
    │  main.py      入口 / CLI 参数 / .env 读取           │
    │  agent.py     核心循环：模型 → 工具 → 再一轮          │
    │  repl.py      交互式控制台（/tools /reload /skills …）│
    │  ui.py        呈现层（PlainUI / RichUI，可替换）      │
    │  loader.py    插件发现 → 导入 → 注册 → 热重载         │
    │  skills.py    技能发现 → SKILL.md → use_skill         │
    │  tools.py     向后兼容垫片（导入即加载插件）           │
    └─────────────────────────────────────────────────────┘
    ┌─ 核心库（插件作者 `import core` 就拿到的全部）────────┐
    │  registry.py  Tool / ToolRegistry / @tool            │
    │  schema.py    自动 Schema 推导                        │
    │  sandbox.py   沙箱 / 输出截断 / 危险命令策略           │
    │  cancel.py    取消令牌 / 杀进程树                     │
    │  images.py    图片附件通道                            │
    │  mcp.py       MCP stdio 客户端 + 桥接                 │
    └─────────────────────────────────────────────────────┘

启动方式：
    python -m core          推荐；需要 src/ 在 sys.path 上
    mini-agent              装了 console script 之后

关于导入风格 —— 请看 _paths.install_import_paths() 的说明：
本包先铺好 sys.path，所以既支持「带包前缀」（from core import tool），
又支持「裸模块名」（import tools / import agent），后者让插件作者写
`from core import tool` 时不必关心目录结构。
"""

from __future__ import annotations

# 必须在任何子模块被导入之前执行 —— 它负责把 sys.path 铺好，
# 这样下面的相对导入和之后的裸模块名导入（import tools 等）都成立。
from ._paths import ROOT, install_import_paths  # noqa: E402

install_import_paths()

__version__ = "1.0.0"

# ---- 核心 API 导出（插件作者只需要 import 这一个包）----
from .cancel import (  # noqa: E402,F401
    Cancelled,
    CancellationToken,
    current_token,
    kill_tree,
    set_current_token,
)
from .images import (  # noqa: E402,F401
    attach_image,
    take_images,
)
from .registry import (  # noqa: E402,F401
    REGISTRY,
    Tool,
    ToolRegistry,
    active_registry,
    call,
    names,
    schemas,
    tool,
    use_registry,
)
from .sandbox import (  # noqa: E402,F401
    WORKSPACE,
    SandboxError,
    check_command,
    clip,
    decode_bytes,
    hexdump,
    looks_binary,
    read_bytes,
    run_process,
    safe_path,
    set_workspace,
    workspace,
)
from .schema import (  # noqa: E402,F401
    json_type,
    param_docs,
    schema_from_callable,
    summary,
)

__all__ = [
    # 包
    "ROOT", "install_import_paths", "__version__",
    # 注册
    "tool", "Tool", "ToolRegistry", "REGISTRY", "call", "names", "schemas",
    "use_registry", "active_registry",
    # 沙箱
    "safe_path", "workspace", "set_workspace", "WORKSPACE", "SandboxError",
    "clip", "read_bytes", "looks_binary", "decode_bytes", "hexdump", "check_command",
    "run_process",
    # 取消
    "Cancelled", "CancellationToken", "current_token", "set_current_token",
    "kill_tree",
    # 图片附件
    "attach_image", "take_images",
    # Schema
    "schema_from_callable", "param_docs", "json_type", "summary",
]