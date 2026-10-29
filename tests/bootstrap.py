"""
测试共用引导 —— 每个 test_*.py 在 import 任何项目模块之前先 `import bootstrap`。

它解决三件事：

  1. **sys.path** —— 测试文件住在 tests/，项目模块在 src/core/，
     不铺路的话 `import agent` / `from core import ...` 都失败。
     这里统一铺好，测试文件里就不必各写一遍 sys.path.insert。

     注意要铺**两个**位置：
       src/               → `import core`
       src/core/          → `import agent` / `import tools` / `from ... import`
     后者是「裸模块名导入」的落点，缺了它老写法全挂。

  2. **MINI_AGENT_WORKSPACE** —— 沙箱根必须是项目根，否则 `demo_case/inbox/x.csv`
     这类相对路径会解析到别处。用 setdefault 而不是直接赋值，是为了让
     外部（run_tests.py / CI）能覆盖它。

  3. **PACKAGE_ROOT 常量** —— 给测试里拼路径用，比每个文件自己算 __file__ 可靠。

注意：本模块**必须**被放在其它项目 import 之前，因为它改的是 sys.path。
写法上永远是：

    import bootstrap  # noqa: F401  —— 必须最先导入
    from core.registry import ToolRegistry
"""

from __future__ import annotations

import os
import sys
import tempfile

#: tests/ 目录
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
#: 项目根目录
PACKAGE_ROOT = os.path.dirname(TESTS_DIR)
#: 源码布局根 src/
SRC_DIR = os.path.join(PACKAGE_ROOT, "src")
#: 真正的包目录 src/core/ —— 裸模块名导入的落点
PKG_DIR = os.path.join(SRC_DIR, "core")

for _p in (SRC_DIR, PKG_DIR, PACKAGE_ROOT, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 沙箱根与环境标识 —— setdefault 让调用方仍可覆盖
os.environ.setdefault("MINI_AGENT_WORKSPACE", PACKAGE_ROOT)
os.environ.setdefault("MINI_AGENT_ROOT", PACKAGE_ROOT)
# 数据目录默认指向一次性临时目录：测试绝不读写用户真实数据（~/.mini-agent）。
# 需要固定路径的用例（settings / models / auth）仍会各自覆写模块内的路径常量。
os.environ.setdefault(
    "MINI_AGENT_DATA_DIR", tempfile.mkdtemp(prefix="miniagent-test-data-")
)

#: mcp_servers.json 的新位置（相对项目根）
MCP_CONFIG = os.path.join(PACKAGE_ROOT, "mcp", "mcp_servers.json")
#: demo 服务端脚本（相对项目根）
MCP_SERVER_SCRIPT = os.path.join("mcp", "mcp_demo_server.py")

__all__ = [
    "TESTS_DIR", "PACKAGE_ROOT", "SRC_DIR", "PKG_DIR",
    "MCP_CONFIG", "MCP_SERVER_SCRIPT",
]
