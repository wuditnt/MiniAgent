# MCP 连接

## 用途

通过 Model Context Protocol 把外部能力挂进同一张工具注册表。一个连接要么是本地进程（stdio），要么是远程 HTTP 服务（Streamable HTTP）。

## 概念

- **配置**：数据目录的 `connectors.json`（默认 `~/.mini-agent/connectors.json`），界面「连接」页读写。首次启动会从安装目录的 `mcp/mcp_servers.json` 迁移一次，只补缺失、不覆盖已有配置。
- **工具命名**：桥接后为 `server__tool`，来源标记为 `mcp`。
- **生命周期**：连接跨 run 复用；取消只停当前请求，不杀进程。保存、启停、重试都会热加载对应连接，**不需要重启后端**。
- **协议范围**：`initialize` 握手 → `tools/list` → `tools/call`；取消以 `notifications/cancelled{requestId, reason}` 告知服务端。
- **工具级开关**：`disabled_tools` 按服务端**原始工具名**记录；被禁用的工具不注册，模型完全看不到。界面卡片显示「已启用 x/y 个工具」。
- **危险标记**：MCP 工具默认危险，任何确认档位都强制弹窗；明知安全的 server 可设 `"dangerous": false`。
- **会话勾选**：新会话默认继承全部已启用连接器（`defaults.connectors = null`），也可在会话里单独勾选；实际可用集合始终与 `enabled` 求交集。

## 配置示例

stdio（本地进程）：

```json
{
  "servers": {
    "filesystem": {
      "display_name": "文件系统",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/some/dir"],
      "cwd": ".",
      "env": {"TOKEN": "..."},
      "timeout": 30,
      "enabled": true,
      "dangerous": true,
      "disabled_tools": []
    }
  },
  "defaults": {"connectors": null}
}
```

HTTP（远程服务）：

```json
{
  "servers": {
    "remote": {
      "transport": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ..."},
      "timeout": 30,
      "enabled": true
    }
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `display_name` / `description` | 界面展示用，留空回退标识 |
| `transport` | `stdio`（默认）或 `http` |
| `command` / `args` / `cwd` / `env` | 仅 stdio；`cwd` 相对路径以工作区为基准 |
| `url` / `headers` | 仅 http；URL 必须是完整的 `http(s)://` |
| `timeout` | 单次请求超时秒数，默认 30 |
| `enabled` | 停用后连接下线、工具一起摘掉 |
| `dangerous` | 默认 `true`（每次调用弹确认） |
| `disabled_tools` | 禁止注册的工具原始名列表 |

### 自带 demo server

`mcp/mcp_demo_server.py` 提供 `now` / `sha256` / `env_info` / `sleep` / `stubborn_sleep` / `cancel_log`，默认关闭。它用于验证协议链路与取消通知测试；测试脚本会自己打开它。

## 传输方式

### stdio

本地子进程，逐行 JSON-RPC。裸解释器名（`python` / `python3` / `python3.x` / `py`）会改写成 `MINI_AGENT_PYTHON` 指定的解释器，`py -3` 的版本参数会被摘掉；子进程 PATH 也前置了解释器目录。

### Streamable HTTP

零依赖实现（`urllib`），行为要点：

- 每个 JSON-RPC 消息一次 POST；响应可能是 `application/json`，也可能是 `text/event-stream`（从 SSE 帧里挑出对应 id 的那条）；
- `initialize` 响应头里的 `Mcp-Session-Id` 会带到后续所有请求；同时发送 `MCP-Protocol-Version`；
- 服务端通知通过 GET 同一地址的长连接 SSE 推送；服务端返回 404/405 时静默降级，不会反复重连；
- 关闭时按规范发 `DELETE` 结束会话，失败忽略；
- 超时与取消语义和 stdio 一致（取消帧、迟到响应按 id 丢弃、`stats` 可观测）。

## 内置模板

`mcp/presets.json` 提供 8 个模板：filesystem、fetch、memory、git、sqlite、time、puppeteer、sequential-thinking。界面「从模板添加」读取 `GET /api/connectors/presets`，自动生成不重名的标识；路径类参数保留 `D:\path\to\folder` 占位，并在抽屉顶部提示替换。

## 失败可见与重试

- 启动失败会记录 `started_ms`（启动耗时）、`attempts`（尝试次数）、`stderr_tail`（stderr 末尾若干行）与完整错误；
- 第一次启动/握手失败会**自动重试一次**（间隔 0.3s），错误信息追加「已自动重试 N 次」；
- 界面失败卡片展示原因、耗时、尝试次数与 stderr，并提供「重试」按钮（`POST /api/connectors/retry`）。

## 服务端通知

连接建立后，客户端读线程把无 id 的协议消息交给管理器：

- `notifications/tools/list_changed`：0.5s 防抖后自动 `reload` 该连接，工具表随服务端变化更新；
- `notifications/message`：按 `[level] data` 写入每连接 100 条的日志环，随连接器快照返回；
- `resources/*`、`prompts/*` 的变更通知暂不消费。

## 密钥掩码与导入导出

- `GET /api/connectors` 与导出文件里的 `env` / `headers` 一律掩码为 `***末4位`（不超过 4 位时全掩码）；
- 保存时值以 `***` 开头表示「保留原值」，服务端用旧值回填；无法回填只发生在导入的新连接器上，会在响应的 `warnings` 里逐项说明；
- `GET /api/connectors/export` 导出脱敏配置（不含会话勾选与全局默认）；
- `POST /api/connectors/import` 逐条校验、写入并热加载，单次最多 50 个，返回每个连接的 `results` 与 `warnings`。

## 取消通知

只在自己这边停止等待是单方面的：对端不知道，会把慢请求跑到底。因此取消时必须补发协议帧：

```json
{"jsonrpc":"2.0","method":"notifications/cancelled",
 "params":{"requestId":3,"reason":"用户取消了本次任务"}}
```

实现要点：

- `requestId` 精确定位，只取消那一个在途请求；
- 幂等，同一 id 只发一次；
- 超时也发；
- 通知不是请求，不等回执，写失败不抛异常；
- 迟到响应按 id 认领并静默丢弃（`client.stats["late_responses"]` 可观测）；
- `reason` 一路留痕：令牌 → 协议帧 → 服务端日志 → UI 提示。

服务端需要配合才成立：收到通知后置位取消事件，工具在检查点退出，且按规范不回响应。

## 并发安全

每个在途请求一个专属队列，响应按 id 精确投递。早期从公共队列取响应、id 不匹配就丢弃的写法会在并发时互相取走响应，表现为只在并发时出现的随机超时；已有回归用例。

## 关键代码位置

- 存储与校验：`src/core/connector_store.py`（`normalize` / `load` / `upsert` / `mask_value` / `merge_masked_env` / `export_payload`）
- 模板：`src/core/connector_presets.py`、`mcp/presets.json`
- 客户端：`src/core/mcp.py` 的 `MCPStdioClient`、`MCPHttpClient`、`MCPServerState`、`fq_tool_name()`
- 工具桥接：`tools_from_client()`；管理器 `MCPManager`（热加载、自动重试、通知调度）；报告 `MCPReport`
- 命令解析：`_resolve_command()` / `_resolve_argv()`
- 接口：`src/core/webui.py` 的 `/api/connectors`、`/test`、`/toggle`、`/retry`、`/presets`、`/export`、`/import`
- demo：`mcp/mcp_demo_server.py`
- 测试：`tests/test_connectors.py`、`tests/test_mcp_http.py`、`tests/test_mcp_cancel.py`、`tests/test_real_mcp_cancel.py`、`tests/test_stream_parallel.py`

## 注意事项

- 解释器必须一致：裸解释器名会改写成本项目的工具解释器（`MINI_AGENT_PYTHON`，未配置时回退后端解释器）；含目录的绝对路径视为显式指定，不会改写。
- 界面上的保存 / 启停 / 重试即时生效；如果手工改了 `connectors.json`，需要重启后端或再触发一次保存来加载。
- 被 `disabled_tools` 禁用的工具不会注册，也不占 schema。
- demo server 默认关闭：6 个工具只服务测试，常开会多占 schema。
- 已知缺口：resources / prompts / sampling 未接入；服务端 `roots/list` 请求无消费者；HTTP 服务端不提供 GET 通知流时，`tools/list_changed` 不会到达（需手动刷新/重试）。见 [known-issues](../04-operations/known-issues.md)。
