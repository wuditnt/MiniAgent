# 后端日志系统实施计划

## 1. 目标

给后端补一套**本地文件日志**：关键路径（启动、HTTP 接口、对话轮次、LLM 调用、工具执行、插件/MCP、记忆提炼、异常）按类别分别落到 `logs/` 下的文件中，便于排障。

约束与已确认的选择：
- 只用 Python 标准库 `logging`，不新增依赖。
- 文件按类别划分：`app.log` / `llm.log` / `tool.log` / `error.log`。
- **不记录 LLM 请求/响应正文**，只记模型、耗时、token 用量、消息条数、字符数等摘要。
- 日志开关、级别、保留天数放在「设置 → 通用配置」，持久化到 `web_data/settings.json`，改动即时生效。
- 控制台现有 `print()` 输出保持不变（日志只落文件，避免双份刷屏）。

## 2. 现状分析

- 全项目 `src/core` **没有任何 `import logging`**，全部是 `print()` + 大量 `except Exception: pass`（`agent.py` 约 20+ 处）。
- `logs/` 目录已存在但只有 `.gitkeep`；README 第 39/675 行已声明其用途，第 770 行把「结构化日志」列为生产化差距。
- 可直接接入的现成信息：
  - [`webui.py#L772`](file:///d:/codeworks/mini-agent/src/core/webui.py#L772) `_RequestHandler.log_message()` 当前是 `pass`，是 HTTP 访问日志的天然挂点。
  - [`webui.py#L775-L784`](file:///d:/codeworks/mini-agent/src/core/webui.py#L775-L784) `_json()` / `_fail()` 是所有 API 响应与错误出口。
  - [`webui.py#L1568-L1613`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1568-L1613) `_run_worker()` 是每条对话的完整生命周期（done/cancelled/error 三类终态）。
  - [`agent.py#L725-L825`](file:///d:/codeworks/mini-agent/src/core/agent.py#L725-L825) `_run_loop()` 每轮 turn、流式空回退、`llm_done` 事件。
  - [`agent.py#L1112-L1130`](file:///d:/codeworks/mini-agent/src/core/agent.py#L1112-L1130) `_call_tool()` / `_invoke()` 已统计工具耗时。
  - [`registry.py#L171-L193`](file:///d:/codeworks/mini-agent/src/core/registry.py#L171-L193) `ToolRegistry.call()` 把所有工具异常收敛成 `[error]` 文本，是工具错误日志的唯一出口。
  - [`loader.py#L119-L183`](file:///d:/codeworks/mini-agent/src/core/loader.py#L119-L183) 插件加载失败已有完整堆栈 `_last_traceback`。
  - [`mcp.py#L409-L410`](file:///d:/codeworks/mini-agent/src/core/mcp.py#L409-L410) MCP 已有 `stderr_tail()`，启动失败写入 `state.error`。
  - [`settings.py#L24-L77`](file:///d:/codeworks/mini-agent/src/core/settings.py#L24-L77) 配置体系（DEFAULTS / RANGES / INT_KEYS / BOOL_KEYS / ENUM_KEYS）可直接扩展。
  - [`webui.py#L1300-L1318`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1300-L1318) 设置保存后调用 `settings.apply_to_runtime()`，可在此触发日志重配置。

## 3. 方案设计

### 3.1 新增 `src/core/logging_setup.py`

职责：集中初始化/重配置日志，业务模块只 `getLogger`。

```python
# 逻辑结构（非最终代码）
_LOG_ROOT = "mini-agent"      # 所有业务 logger 的父级
_CATEGORIES = ("app", "llm", "tool")

def init(log_dir: str | None = None, level: str = "INFO",
         retention_days: int = 7, enabled: bool = True) -> None: ...
def reconfigure() -> None: ...        # 从 settings 读值，关闭旧 handler 后重建
def get_logger(category: str): ...    # "app" | "llm" | "tool" → mini-agent.<category>
```

- 文件：
  - `logs/app.log` — 启动/关闭、HTTP API、设置变更、会话生命周期、记忆提炼。
  - `logs/llm.log` — 每轮模型调用摘要、流式降级、重试、token 用量。
  - `logs/tool.log` — 工具开始/结束、耗时、结果字符数、错误。
  - `logs/error.log` — 任何类别 `ERROR` 及以上（挂在父 logger 上的独立 handler，用于快速定位异常）。
- handler：`logging.handlers.TimedRotatingFileHandler(when="midnight", backupCount=retention_days, encoding="utf-8", delay=True)`；`formatter` 为
  `%(asctime)s %(levelname)-5s [%(threadName)s] %(name)s %(message)s`，`datefmt="%Y-%m-%d %H:%M:%S"`。
- 级别：类别 handler 用设置里的 `log_level`；`error.log` 固定 `ERROR`。
- 幂等：模块级 `_initialized` + `threading.Lock`；`reconfigure()` 先 `removeHandler` + `close()` 再重建，避免设置多次保存后重复写。
- 未初始化时（单测直接 import 业务模块）：给 `mini-agent` 挂 `NullHandler`，日志静默而不报 "No handlers"。
- `enabled=False` 时只挂 `NullHandler`，不创建文件。
- `init(log_dir=...)` 支持测试指向临时目录。

内容规范：
- 用户输入/模型输出**只记字符数**，不记正文。
- 工具参数预览：复用 `core.ui.brief()` 思路，截断到 200 字符（路径/命令是排障必需）。
- 异常：用 `logger.exception(...)` 或 `exc_info=True`，保证 `error.log` 有堆栈。

### 3.2 `settings.py` 扩展

在 `DEFAULTS` 增加：

| key | 默认 | 类型 | 约束 |
| --- | --- | --- | --- |
| `log_enabled` | `True` | bool | 加入 `BOOL_KEYS` |
| `log_level` | `"INFO"` | enum | `ENUM_KEYS = ("DEBUG","INFO","WARNING","ERROR")` |
| `log_retention_days` | `7` | int | `RANGES = (1, 90)`，加入 `INT_KEYS` |

`apply_to_runtime()` 末尾调用 `logging_setup.reconfigure()`（惰性 import，避免循环依赖）。

### 3.3 `main.py`

- `main()` 中 `settings.apply_to_runtime()` 之后立即 `logging_setup.init()`（幂等，reconfigure 已完成实际配置）。
- 启动时写 `app` INFO：`启动 mode=web/console/prompt workspace=... model=... host=... port=...`。
- `_run_web()`：`OSError` 分支用 `logger.exception` 记录端口占用/绑定失败（保留现有 print）。
- `_run_prompt_once()`：结束时记录 usage 摘要。

### 3.4 `webui.py`

- `log_message()`：改为 `app` DEBUG 记录 `"%s %s"`（method/path/status），默认 INFO 级别下不刷屏；排查时把级别调到 DEBUG 即可看到访问日志。
- `_fail()`：记录 `app` WARNING：状态码 + 路由 + 错误消息。
- `_run_worker()`：
  - 开始：`chat.start session=... workspace=... mode=... approval=... chars=...`
  - 成功：`chat.done session=... sec=... usage=... answer_chars=...`
  - `Cancelled`：`chat.cancelled session=... reason=...`
  - `Exception`：`chat.error session=...` + `exc_info=True`（进 error.log）
- `WebServer.serve_forever()`：记录监听地址；`serve()` 入口调用 `logging_setup.init()` 兜底（测试/直接调用场景）。
- `_handle_settings_put()`：`settings.update()` 成功后调用 `logging_setup.reconfigure()`（`apply_to_runtime()` 内已调用，此处无需重复，仅确认顺序）。

### 3.5 `agent.py`

- 模块级：`_log = logging.getLogger("mini-agent.llm")`、`_tlog = logging.getLogger("mini-agent.tool")`、`_alog = logging.getLogger("mini-agent.app")`。
- `run()`：任务开始/结束摘要（输入字符数、附件数、轮数、usage 增量）。
- `_run_loop()`：
  - 每轮 `turn`、消息条数、模型名、耗时、usage、`tool_calls` 数量、是否流式。
  - 流式空回退：WARNING。
  - 达到 `max_turns`：WARNING。
- `_request()`：`stream_options` / `stream` 降级重试各记 WARNING（含异常类型）；连接类异常 `exc_info=True` 记 ERROR（对应 `APIConnectionError` 排查）。
- `_trim_context()`：发生上下文收敛时 INFO（前后字符数）。
- `_run_tools()`：并行/串行、工具数量、本轮总耗时。
- `_call_tool()`：`tool.start name=... args=<200字预览>`；成功 `tool.end name=... sec=... chars=...`；结果以 `[error]` 开头时 WARNING。
- `_spawn_extract()`：记忆提炼成功/失败（失败 `exc_info=True`），不阻塞主流程。

### 3.6 `registry.py`

`ToolRegistry.call()` 的异常分支统一记 `tool` 日志：
- `Cancelled` → INFO `tool.cancelled name=...`
- `SandboxError` / `TypeError` → WARNING
- 其他 `Exception` → ERROR + `exc_info=True`
保持返回值不变（模型侧行为零变化）。

### 3.7 `loader.py` / `mcp.py`

- `PluginLoader.load_one()` 失败：ERROR + `last_traceback()` 全文。
- `MCPManager.start()` 及各 server 启动失败：ERROR + `state.error` + `stderr_tail()`。

### 3.8 前端设置页

- [`web/src/types.ts#L164-L180`](file:///d:/codeworks/mini-agent/web/src/types.ts#L164-L180) `GeneralSettings` 增加：
  ```ts
  log_enabled: boolean;
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  log_retention_days: number;
  ```
- [`SettingsModal.tsx`](file:///d:/codeworks/mini-agent/web/src/components/SettingsModal.tsx#L259-L331)「通用配置」在「沙箱读取范围」之后新增一组「运行日志」：
  - `Switch`：启用文件日志（`log_enabled`）
  - `Select`：级别（DEBUG / INFO / WARNING / ERROR，默认 INFO）
  - `InputNumber`：保留天数（1–90，默认 7）
  - 说明文案：日志位置 `logs/`，按类别分文件；不记录对话正文。
  - 沿用现有 `patch()` + `dirty` + 保存按钮流程，不新增保存逻辑。
- 不新增 CSS（复用 `.gs-section` / `.gs-group-title`）。

### 3.9 测试与文档

- 新增 `tests/test_logging.py`：
  1. `init(log_dir=tmp)` 后四个文件被创建，写入各 logger 后内容分别落到对应文件。
  2. `ERROR` 同时出现在类别文件和 `error.log`。
  3. `reconfigure()` 不产生重复 handler（记录 handler 数量）。
  4. `enabled=False` 时不创建文件。
  5. `settings` 三个新键可校验：非法级别/超范围天数报错。
- `tests/run_tests.py` 增加套件条目（免费套件）。
- README：在「Web UI 要点」或配置章节补一段日志说明（目录、四个文件、开关位置、正文不落盘）。

## 4. 决策与假设

- **不接入控制台 handler**：现有 `print()`/UI 已经承担终端展示，日志只写文件，避免重复。
- **单进程假设**：当前是单进程 `ThreadingHTTPServer`；多线程写入由 `logging` 自身锁保证安全。未来多进程再考虑 `QueueHandler`。
- **保留策略按天**：`TimedRotatingFileHandler` 每天切分，`backupCount=log_retention_days`；不按大小切分（项目日志量小）。
- **不记录 LLM 正文**：只记摘要；如后续需要复盘，再单独加 DEBUG 开关（本次不做）。
- **HTTP 访问日志默认不显示**：INFO 级别下 `log_message` 的 DEBUG 记录不会落盘，保持现有「不刷屏」体验。

## 5. 实施顺序

1. `logging_setup.py` + `settings.py` 新键（可独立验证）。
2. `main.py` 初始化 + 启动日志。
3. `webui.py`（HTTP、设置、会话生命周期）。
4. `agent.py`（LLM / 工具 / 记忆）。
5. `registry.py` / `loader.py` / `mcp.py`。
6. 前端类型 + 设置页。
7. `tests/test_logging.py` + `run_tests.py` + README。

## 6. 验证

- 单元测试：`.venv\Scripts\python.exe tests\run_tests.py`（新增日志套件 + 全量回归）。
- 前端：`web` 目录 `npm.cmd run build`（含 `tsc`）。
- 手工冒烟：
  1. `python main.py --web --host 0.0.0.0 --port 8000`
  2. 发一条对话 → 检查 `logs/app.log` / `llm.log` / `tool.log` 生成且有对应记录。
  3. 触发一次工具报错（如调用不存在文件）→ `tool.log` WARNING + `error.log`。
  4. 设置页改级别为 DEBUG → 保存后立即生效（访问日志出现）；改回 INFO → 不再刷。
  5. 设置 `log_enabled=false` → 保存后不再写新文件（已有文件保留）。
- 回归点：控制台输出与 Web UI 行为不变；测试环境不产生 `logs/` 污染（测试用临时目录）。
