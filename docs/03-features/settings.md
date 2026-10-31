# 通用配置与运行日志

## 用途

系统配置集中管理运行时行为：模型、通用参数、联网搜索、记忆、工具。配置存 `~/.mini-agent/settings.json`，保存即生效，无需重启。

## 入口

左侧抽屉底部「系统配置」按钮；顶栏点模型名也能进（直接落在模型配置页）。五个页签：

1. **模型配置** —— 见 [模型与推理等级](models-and-reasoning.md)
2. **通用配置** —— 本文
3. **联网搜索** —— 见 [联网搜索](web-search.md)
4. **记忆** —— 见 [记忆系统](memory.md)
5. **工具管理** —— 见 [工具系统](tools.md)

## 通用配置项

| 分组 | 项 | 说明 |
|---|---|---|
| 循环 | 最大轮数 | 用尽后自动收尾 |
| 上下文 | 上下文字符上限 | 输入区水位条的上限 |
| 上下文 | 保留最近轮数 | 硬截断窗口 |
| 工具 | 单次工具输出上限 | 默认 8000 字符 |
| 模型 | 温度 / 流式输出 / 并行工具 / 并发线程数 | 运行时可调 |
| 工具 | 工具档位 `standard` / `full` | standard 低频工具按需检索 |
| 沙箱 | 沙箱读取范围 `strict` / `whitelist` / `open` | 白名单每行一条；写入永远硬禁 |
| 记忆 | 启用记忆 / 自动提炼 / 注入预算 | 见记忆文档 |
| 运行轨迹 | 记录会话事件流 `events_enabled` | 默认开 |
| 子智能体 | 开关 / 子任务轮次 / 超时 | 默认超时 120s |
| 运行日志 | 启用 / 级别 / 保留天数 | 默认 INFO / 7 天 |

页面底部提供「恢复默认」。

## 取值优先级

**命令行显式参数 > 这里保存的值 > 环境变量 > 内置默认**。

`.env` / 环境变量只放系统配置（监听地址、端口、路径、输出上限等），API Key 不放这里。

常用环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `MINI_AGENT_PORT` / `MINI_AGENT_HOST` | 监听端口 / 地址 | 8000 / 0.0.0.0 |
| `MINI_AGENT_DATA_DIR` | 数据目录 | `~/.mini-agent` |
| `MINI_AGENT_WORKSPACE` | 沙箱根 | 当前用户数据目录 |
| `MINI_AGENT_MAX_OUTPUT` | 单次工具输出上限 | 8000 |
| `MINI_AGENT_STREAM` / `MINI_AGENT_PARALLEL` | 流式 / 并行 | 1 / 1 |
| `MINI_AGENT_MAX_WORKERS` | 并行线程数 | 4 |
| `MINI_AGENT_MAX_TURNS` | 最大轮数 | 10 |
| `MINI_AGENT_ALLOWED_HOSTS` | 追加允许的 Host | 空 |
| `MINI_AGENT_ALLOW_DANGEROUS` | 放开危险命令拦截 | 0 |

旧名 `AGENT_*` 仍可读。

## 运行日志

后端日志只落本地文件，按天一个目录、按类别分文件：

```text
~/.mini-agent/logs/
  2026-10-17/
    app.log  llm.log  tool.log  error.log
```

| 文件 | 内容 |
|---|---|
| `app.log` | 启动与监听地址、HTTP 接口错误、会话生命周期、工具 / MCP 加载失败、记忆提炼 |
| `llm.log` | 每轮模型调用摘要（耗时、消息数、token 用量、是否流式）、流式降级与重试 |
| `tool.log` | 工具开始 / 结束、耗时、结果字符数、错误 |
| `error.log` | 所有 ERROR 及以上（带堆栈），任何类别都汇总到这里 |

- **不记录对话正文**：输入输出只记字符数；工具参数只记 200 字预览。
- 跨天自动切目录；超过保留天数的旧目录自动清理。
- `DEBUG` 额外记录每条 HTTP 请求；`INFO` 下访问日志不落盘。
- 配置项：`log_enabled` / `log_level` / `log_retention_days`，保存即时生效。

## 关键代码位置

- 配置读写：`src/core/settings.py` 的 `keys()` / `coerce()` / `get()` / `get_all()` / `defaults()` / `update()` / `reset()` / `apply_to_runtime()`
- 运行时应用：`src/core/agent.py` 的 `_setting()` / `_pick()`；`src/core/sandbox.py` 的 `set_read_policy()` / `set_max_output_chars()`
- 日志：`src/core/logging_setup.py`（按日目录、四类文件、保留清理、幂等重配）
- 接口：`src/core/webui.py` 的 `_handle_settings_get()` / `_handle_settings_put()` / `_settings_public()`
- 前端：`web/src/components/SettingsModal.tsx`
- 测试：`tests/test_logging.py`、`tests/test_webui.py`（通用配置用例）

## 注意事项

- 配置保存即生效；但工具模块与 MCP 配置的改动仍需重启后端。
- 「恢复默认」只重置通用配置，不动模型配置与账号。
- 日志里没有对话正文，排障时结合事件流（运行轨迹）看模型实际收到了什么。
- 命令行参数优先级最高，适合临时覆盖，不要和持久配置混着排查。
