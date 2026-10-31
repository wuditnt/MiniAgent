# MCP 连接

## 用途

通过 Model Context Protocol 把外部进程的能力挂进同一张工具注册表。连接 = 一个外部 MCP server，每个连接是一个独立进程。

## 概念

- **配置**：`mcp/mcp_servers.json` 的 `servers` 段，每个 server 一段。
- **工具命名**：桥接后为 `server__tool`，来源标记为 `mcp`。
- **生命周期**：服务端进程跨 run 复用；取消不会把它杀掉。
- **协议范围**：`initialize` 握手 → `tools/list` → `tools/call`；取消以 `notifications/cancelled{requestId, reason}` 告知服务端。
- **危险标记**：MCP 工具默认危险，任何确认档位都强制弹窗；明知安全的 server 可设 `"dangerous": false`。

## 配置示例

```json
{
  "servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/some/dir"],
      "enabled": true,
      "dangerous": true,
      "group": "MCP:fs",
      "timeout": 30
    }
  }
}
```

启动日志会打印各 server 连接状态；改完重启后端重新挂载。

### 自带 demo server

`mcp/mcp_demo_server.py` 提供 `now` / `sha256` / `env_info` / `sleep` / `stubborn_sleep` / `cancel_log`，默认关闭。它用于验证协议链路与取消通知测试；测试脚本会自己打开它。

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

- 客户端：`src/core/mcp.py` 的 `MCPStdioClient`、`MCPServerState`、`fq_tool_name()`
- 工具桥接：`tools_from_client()`
- 配置装载：`load_config()`；管理器 `MCPManager`；报告 `MCPReport`
- 命令解析：`_resolve_command()`（`python` / `python3` / `python.exe` 改写为 `sys.executable`）
- demo：`mcp/mcp_demo_server.py`
- 测试：`tests/test_mcp_cancel.py`、`tests/test_real_mcp_cancel.py`、`tests/test_stream_parallel.py`

## 注意事项

- 解释器必须一致：`"command": "python"` 只在恰好是 `python` / `python3` / `python.exe` 时改写成当前解释器；想固定解释器就写绝对路径。
- 改配置后必须重启后端，MCP 不热重载。
- 连接跨 run 复用，取消只停当前请求，不杀进程。
- demo server 默认关闭：6 个工具只服务测试，常开会多占 schema。
- 已知缺口：resources / prompts / sampling 未接入；服务端主动通知（progress、roots/list）无消费者。见 [known-issues](../04-operations/known-issues.md)。
