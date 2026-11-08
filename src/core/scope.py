"""按会话生效的工具可见范围。

WebUI 每轮开始时把本会话勾选的连接器写进 ContextVar；
子代理（delegate）在同一线程里跑，借此继承父会话的范围，避免绕开勾选。
值：None = 不限制；集合 = 只允许这些 MCP 模块（mcp:<name>）。
"""
import contextvars

connector_scope: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "connector_scope", default=None
)
