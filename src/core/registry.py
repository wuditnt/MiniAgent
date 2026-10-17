"""
工具注册表：整个 Agent 的能力都挂在这里。

三方（内置工具 / 你的插件 / MCP 外部工具）最终都会变成同一个 `Tool` 对象，
进入同一个 REGISTRY。对 Agent 循环来说，它们没有任何区别 —— 这正是「可扩展」的含义。

一个工具需要什么？
    name        模型看到的函数名（必须匹配 ^[A-Za-z0-9_-]{1,64}$）
    description 模型靠它决定什么时候用这个工具
    parameters  JSON Schema，描述参数
    func        真正执行的 Python 函数
附加元信息（不发给模型，供控制台与权限判断使用）：
    group / dangerous / source / module / origin / tags
"""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from .cancel import Cancelled
from .logging_setup import get_logger
from .sandbox import SandboxError, clip
from .schema import merge_params, schema_from_callable, summary

VALID_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_log = get_logger("tool")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]
    group: str = "通用"
    dangerous: bool = False
    source: str = "core"          # core / builtin / user / mcp
    module: str = ""              # 定义它的模块名（热重载用）
    origin: str = ""              # 人类可读来源，例如 MCP server 名
    tags: tuple[str, ...] = field(default_factory=tuple)
    exclusive: bool = False       # 独占：与本批其它工具不能并行执行（见 agent._run_tools）
    no_clip: bool = False         # 不截断输出：给「正文即产品」的工具用（如 use_skill 返回技能全文）

    def schema(self) -> dict:
        """导出成 OpenAI function-calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def signature(self) -> str:
        """给控制台看的简短签名，例如 read_file(path, offset=1)。"""
        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        parts = []
        for pname, spec in props.items():
            if pname in required:
                parts.append(pname)
            else:
                default = spec.get("default")
                parts.append(f"{pname}={default!r}" if default is not None else f"{pname}=…")
        return f"{self.name}({', '.join(parts)})"


def _default_group(module: str) -> str:
    """plugins.fs_tools -> fs ；plugins_user.git_tools -> git"""
    leaf = module.rsplit(".", 1)[-1]
    return leaf[:-6] if leaf.endswith("_tools") else leaf


def _default_source(module: str) -> str:
    if module.startswith("plugins_user"):
        return "user"
    if module.startswith("plugins"):
        return "builtin"
    return "core"


class ToolRegistry:
    """工具的唯一存放处。支持按来源/分组检索，以及按模块热删除。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.skipped: dict[str, str] = {}   # 注册失败的记录：name -> 原因

    # ---------------------------------------------------------- 增删查

    def add(self, t: Tool, overwrite: bool = False) -> Tool:
        if not VALID_NAME.match(t.name):
            raise ValueError(
                f"工具名不合法：{t.name!r}（只允许字母数字下划线连字符，长度 1-64）"
            )
        if t.name in self._tools and not overwrite:
            raise ValueError(f"工具名冲突：{t.name}（已被 {self._tools[t.name].module} 注册）")
        self._tools[t.name] = t
        self.skipped.pop(t.name, None)
        return t

    def remove(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def remove_module(self, module: str) -> int:
        """移除某个模块注册的全部工具 —— 热重载时先清后建。"""
        doomed = [n for n, t in self._tools.items() if t.module == module]
        for n in doomed:
            del self._tools[n]
        return len(doomed)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # ---------------------------------------------------------- 检索

    def all(self, group: str | None = None, source: str | None = None) -> list[Tool]:
        out = list(self._tools.values())
        if group:
            out = [t for t in out if t.group == group]
        if source:
            out = [t for t in out if t.source == source]
        return sorted(out, key=lambda t: (t.group, t.name))

    def groups(self) -> dict[str, list[Tool]]:
        result: dict[str, list[Tool]] = {}
        for t in self.all():
            result.setdefault(t.group, []).append(t)
        return result

    def sources(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for t in self._tools.values():
            counter[t.source] = counter.get(t.source, 0) + 1
        return counter

    def modules(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for t in self._tools.values():
            result.setdefault(t.module or "(unknown)", []).append(t.name)
        return result

    def find(self, keyword: str) -> list[Tool]:
        """按名字 / 描述 / 分组 / 来源过滤 —— 让 /tools fs 这种用法成立。"""
        k = keyword.lower()
        return [
            t for t in self.all()
            if k in t.name.lower()
            or k in t.description.lower()
            or k in t.group.lower()
            or k == t.source.lower()
        ]

    # ---------------------------------------------------------- 对模型接口

    def schemas(self, exclude: set[str] | None = None) -> list[dict]:
        exclude = exclude or set()
        return [t.schema() for n, t in sorted(self._tools.items()) if n not in exclude]

    def call(self, name: str, arguments: dict[str, Any] | None) -> str:
        """执行工具。任何异常都转成文本还给模型，绝不让主循环崩掉。

        Cancelled 单独处理：转成 [cancelled] 而不是 [error] ——
        「用户取消」不是失败，模型看到它应该收手并汇报，而不是报错重试。
        """
        t = self._tools.get(name)
        if t is None:
            _log.warning("tool.unknown name=%s", name)
            return f"[error] 未知工具：{name}"
        args = arguments if isinstance(arguments, dict) else {}
        try:
            result = t.func(**args)
        except Cancelled:
            _log.info("tool.cancelled name=%s", name)
            return "[cancelled] 用户取消了本次任务，该工具未完成执行"
        except SandboxError as e:
            _log.warning("tool.sandbox_denied name=%s reason=%s", name, e)
            return f"[error] {e}"
        except TypeError as e:
            _log.warning("tool.bad_args name=%s error=%s", name, e)
            return f"[error] 参数不匹配：{e}"
        except Exception as e:  # noqa: BLE001 —— 故意兜住一切
            _log.exception("tool.crashed name=%s", name)
            return f"[error] {type(e).__name__}: {e}"
        # 截断是默认策略（防止工具刷爆上下文）；声明 no_clip 的工具例外 ——
        # 它的返回值就是给模型的完整正文（如技能指引），截掉反而坏了语义。
        return result if t.no_clip else clip(result)


# 全局注册表：所有插件、内置工具、MCP 工具都注册到这里
REGISTRY = ToolRegistry()

# 插件模块里的 `@tool` 默认往哪注册。
# 正常情况下就是 REGISTRY；PluginLoader 会在导入插件期间临时切到它自己的注册表，
# 这样「把插件装进指定注册表」才真正成立（而不是嘴上说说）。
_ACTIVE: ToolRegistry = REGISTRY
_ACTIVE_LOCK = threading.RLock()


def active_registry() -> ToolRegistry:
    """当前 `@tool` 会注册到的注册表。"""
    return _ACTIVE


@contextmanager
def use_registry(reg: ToolRegistry):
    """临时切换默认注册表。插件导入期间使用。"""
    global _ACTIVE
    with _ACTIVE_LOCK:
        previous = _ACTIVE
        _ACTIVE = reg
    try:
        yield reg
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE = previous


# ---------------------------------------------------------------- 装饰器

def tool(
    name: str | Callable | None = None,
    description: str | None = None,
    *,
    params: dict | None = None,
    required: list[str] | None = None,
    group: str | None = None,
    dangerous: bool = False,
    exclusive: bool = False,
    no_clip: bool = False,
    source: str | None = None,
    origin: str = "",
    tags: tuple[str, ...] = (),
    registry: ToolRegistry | None = None,
):
    """把一个普通函数注册成模型可调用的工具。

    三种用法：
        @tool                                    # 全自动：名字取自函数名，schema 取自类型注解
        @tool("read_file", "读取文件")             # 显式名字与描述
        @tool(name="x", params={...})             # 覆盖部分参数描述

    exclusive=True 表示「本轮里只要有它，整批工具都串行执行」——
    给会改动共享状态（如切分支、改全局配置、写同一个文件）的工具用。
    no_clip=True 表示「返回值不截断」—— 给返回完整正文的工具用（如技能指引），
    默认情况下超长输出会被截到 AGENT_MAX_OUTPUT 字符。
    """
    # 注意：不能写 `registry or REGISTRY`。
    # ToolRegistry 定义了 __len__，空注册表是 falsy，会被静默替换成全局注册表。
    reg = registry if registry is not None else _ACTIVE

    def _build(fn: Callable) -> Callable:
        module = getattr(fn, "__module__", "") or ""
        auto = schema_from_callable(fn)
        parameters = merge_params(auto, params, required)

        t = Tool(
            name=name if isinstance(name, str) and name else fn.__name__,
            description=description or summary(fn),
            parameters=parameters,
            func=fn,
            group=group or _default_group(module),
            dangerous=dangerous,
            exclusive=exclusive,
            no_clip=no_clip,
            source=source or _default_source(module),
            module=module,
            origin=origin,
            tags=tuple(tags),
        )
        try:
            reg.add(t)
        except ValueError as e:
            # 注册失败不抛异常 —— 一个坏插件不该阻断整个程序启动
            reg.skipped[t.name] = str(e)
            return fn
        setattr(fn, "__tool__", t)
        return fn

    # 裸装饰器写法：@tool
    if callable(name) and not isinstance(name, str):
        fn = name
        name = None
        return _build(fn)

    return _build


# ---------------------------------------------------------------- 便捷函数

def schemas(exclude: set[str] | None = None) -> list[dict]:
    return REGISTRY.schemas(exclude)


def call(name: str, arguments: dict[str, Any] | None) -> str:
    return REGISTRY.call(name, arguments)


def names() -> list[str]:
    return REGISTRY.names()
