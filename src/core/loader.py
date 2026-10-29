"""
插件加载器：发现 → 导入 → 注册 → 热重载。

设计原则：
  1. 约定优于配置 —— 只要是 plugins/ 或 plugins_user/ 下的 .py（或含 __init__.py 的子目录），
     就会被自动发现，无需注册表文件、无需改核心代码。
  2. 故障隔离 —— 某个插件导入失败只记录在报告里，不影响其它插件和主程序启动。
  3. 可热重载 —— 改完插件不用重启，在控制台敲 /reload 即可（开发体验的决定性差异）。
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback
from dataclasses import dataclass, field

from core import envs
from core.logging_setup import get_logger
from core.registry import REGISTRY, ToolRegistry, use_registry

DEFAULT_PLUGIN_DIRS = ("plugins", "plugins_user")

_log = get_logger("app")


def default_dirs() -> list[str]:
    """插件目录。可用环境变量 AGENT_PLUGIN_DIRS 覆盖（逗号或分号分隔）。

    注意：当自定义目录时会**替换**默认目录，而不是追加 —— 这样行为可预测。
    想追加就写全：AGENT_PLUGIN_DIRS=plugins,plugins_user,my_plugins

    设 AGENT_NO_PLUGINS=1 可完全关闭插件加载（调试核心循环时有用）。
    """
    if envs.get_bool("MINI_AGENT_NO_PLUGINS", False, alias="AGENT_NO_PLUGINS"):
        return []
    raw = envs.get_str("MINI_AGENT_PLUGIN_DIRS", alias="AGENT_PLUGIN_DIRS")
    if not raw:
        return list(DEFAULT_PLUGIN_DIRS)
    return [d.strip() for d in raw.replace(";", ",").split(",") if d.strip()]


@dataclass
class LoadReport:
    loaded: list[str] = field(default_factory=list)        # 本次成功加载的模块
    reloaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    tools: dict[str, list[str]] = field(default_factory=dict)  # module -> [tool names]
    failed: dict[str, str] = field(default_factory=dict)   # module -> 错误信息
    skipped: dict[str, str] = field(default_factory=dict)  # tool name -> 冲突原因

    @property
    def tool_count(self) -> int:
        return sum(len(v) for v in self.tools.values())

    def summary(self) -> str:
        bits = [f"模块 {len(self.loaded) + len(self.reloaded)} 个", f"工具 {self.tool_count} 个"]
        if self.reloaded:
            bits.append(f"重载 {len(self.reloaded)}")
        if self.removed:
            bits.append(f"移除 {len(self.removed)}")
        if self.failed:
            bits.append(f"失败 {len(self.failed)}")
        if self.skipped:
            bits.append(f"冲突 {len(self.skipped)}")
        return " | ".join(bits)


class PluginLoader:
    """管理若干个插件目录。"""

    def __init__(
        self,
        project_root: str,
        dirs: list[str] | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.root = os.path.abspath(project_root)
        self.dirs = list(dirs) if dirs else default_dirs()
        # 不能写 `registry or REGISTRY`：ToolRegistry 有 __len__，空注册表是 falsy
        self.registry = registry if registry is not None else REGISTRY
        self._loaded: dict[str, object] = {}   # module name -> module object
        self._last_traceback = ""              # 最近一次导入失败的完整堆栈（诊断用）

    # ------------------------------------------------------------ 内部

    def _ensure_on_path(self) -> None:
        if self.root not in sys.path:
            sys.path.insert(0, self.root)

    def _record(self, module_name: str, report: LoadReport) -> None:
        tools = [t.name for t in self.registry.all() if t.module == module_name]
        report.tools[module_name] = tools
        if not tools and module_name not in report.failed:
            report.failed[module_name] = "模块加载成功，但没有注册任何工具（是否忘了 @tool？）"

    # ------------------------------------------------------------ 发现

    def discover(self) -> list[str]:
        """扫描插件目录，返回可导入的模块名，如 plugins.fs_tools。"""
        self._ensure_on_path()
        found: list[str] = []
        for d in self.dirs:
            base = os.path.join(self.root, d)
            if not os.path.isdir(base):
                continue
            if not os.path.isfile(os.path.join(base, "__init__.py")):
                # 不是包 —— 跳过并提示，避免出现"扔了文件却没生效"的困惑
                continue
            for entry in sorted(os.listdir(base)):
                if entry.startswith("_") or entry.startswith("."):
                    continue
                full = os.path.join(base, entry)
                if os.path.isfile(full) and entry.endswith(".py"):
                    found.append(f"{d}.{entry[:-3]}")
                elif os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
                    found.append(f"{d}.{entry}")
        return found

    # ------------------------------------------------------------ 加载

    def load_all(self) -> LoadReport:
        report = LoadReport()
        for module_name in self.discover():
            self._import(module_name, report)
        report.skipped = dict(self.registry.skipped)
        return report

    def load_one(self, module_name: str) -> LoadReport:
        report = LoadReport()
        self._import(module_name, report)
        report.skipped = dict(self.registry.skipped)
        return report

    def _import(self, module_name: str, report: LoadReport) -> None:
        self._ensure_on_path()
        importlib.invalidate_caches()
        # 重载前先把旧工具摘下来（否则同名注册会撞车），但留一份快照：
        # 重载失败（语法错 / 导入期异常）时要把它们原样装回去 ——
        # 一次失败的 /reload 不该把插件的能力整片清空。
        snapshot = [t for t in self.registry.all() if t.module == module_name]
        try:
            # 关键：在导入/重载期间把默认注册表切到 self.registry，
            # 插件里的 `@tool` 才会注册进我们指定的注册表。
            with use_registry(self.registry):
                if module_name in sys.modules and module_name in self._loaded:
                    self.registry.remove_module(module_name)
                    module = importlib.reload(sys.modules[module_name])
                    report.reloaded.append(module_name)
                else:
                    module = importlib.import_module(module_name)
                    report.loaded.append(module_name)
            self._loaded[module_name] = module
            self._record(module_name, report)
        except Exception as e:  # noqa: BLE001 —— 插件炸了不能带走主程序
            self._restore(module_name, snapshot)
            tb = traceback.format_exc().strip().splitlines()
            report.failed[module_name] = f"{type(e).__name__}: {e}"
            self._last_traceback = "\n".join(tb)
            _log.error("plugin.load_failed module=%s\n%s", module_name, self._last_traceback)

    def _restore(self, module_name: str, tools: list) -> None:
        """把重载前的旧工具装回去（只在本模块已被注册表接纳时生效）。"""
        if not tools:
            return
        for t in tools:
            try:
                self.registry.add(t)
            except ValueError:
                # 重载过程中已经注册出同名工具（半成功）→ 以新的为准
                continue
        _log.warning("plugin.reload_restored module=%s tools=%d", module_name, len(tools))

    # ------------------------------------------------------------ 热重载

    def reload(self, module_name: str | None = None) -> LoadReport:
        """重载指定插件；不传则重载全部，并处理新增/删除的插件文件。"""
        report = LoadReport()
        on_disk = set(self.discover())

        # 1) 磁盘上已经被删掉的插件 —— 把它的工具摘掉
        for gone in [m for m in self._loaded if m not in on_disk]:
            self.registry.remove_module(gone)
            self._loaded.pop(gone, None)
            # 同时清掉 sys.modules：否则用户把同名文件加回来时，_import 会拿到
            # 上一次的旧模块对象（既不重新执行文件，也不注册新工具）。
            sys.modules.pop(gone, None)
            report.removed.append(gone)

        targets = [module_name] if module_name else sorted(on_disk)
        for m in targets:
            if m not in on_disk:
                report.failed[m] = "找不到该插件模块（检查文件名拼写）"
                continue
            self._import(m, report)

        report.skipped = dict(self.registry.skipped)
        return report

    # ------------------------------------------------------------ 查询

    def loaded_modules(self) -> list[str]:
        return sorted(self._loaded)

    def last_traceback(self) -> str:
        """最近一次插件导入失败的完整堆栈；没有失败过则为空串。"""
        return self._last_traceback

    def module_info(self, module_name: str) -> dict:
        mod = self._loaded.get(module_name)
        info = getattr(mod, "PLUGIN_INFO", None) if mod else None
        return {
            "module": module_name,
            "file": getattr(mod, "__file__", ""),
            "doc": ((getattr(mod, "__doc__", "") or "").strip().splitlines() or [""])[0],
            "info": info or {},
            "tools": [t.name for t in self.registry.all() if t.module == module_name],
        }


def load_plugins(project_root: str, dirs: list[str] | None = None) -> LoadReport:
    """便捷入口：一次性加载全部插件。"""
    return PluginLoader(project_root, dirs).load_all()
