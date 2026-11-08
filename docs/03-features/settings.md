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
| 外观 | 主题 | 跟随系统 / 浅色 / 深色（仅本机保存，见下节） |
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

页面底部提供「恢复默认」。该按钮只重置写入 `settings.json` 的运行项，不影响外观主题。

## 外观与主题

支持三档：**跟随系统 / 浅色 / 深色**，默认跟随系统。

两个入口，改的是同一份状态：

1. 系统配置 → 通用配置 → 外观 → 主题
2. 顶栏右上角用户菜单 → 主题（子菜单带勾选）

行为约定：

- **只存本机**：写在浏览器 `localStorage["ma-theme"]`，不写 `settings.json`、不随账号同步 —— 手机想深色、电脑想浅色是常见需求，且未登录时也能生效。
- **即时生效**：不需要点保存，也不影响正在进行的对话。
- **跟随系统**会监听 `prefers-color-scheme`，系统切换深浅色时页面实时跟随；同时更新 `<meta name="theme-color">`，手机状态栏颜色同步。
- **首帧无闪白**：`index.html` 里有一段内联脚本，在 React 加载前就把 `data-theme` 写到 `<html>` 上。
- 深色覆盖范围：antd 组件走 `darkAlgorithm`；自定义样式全部改用语义调色板变量（`--bg-*` / `--tx-*` / `--bd-*`），深色只需在 `theme-dark.css` 覆盖变量；代码高亮切 `oneDark`，Mermaid / ECharts 使用深色主题。
- 图片、PDF、HTML 预览的**内容本身**不做颜色反转（预览即所见）。

## 取值优先级

**命令行显式参数 > 这里保存的值 > 环境变量 > 内置默认**。

`.env` / 环境变量只放系统配置（监听地址、端口、路径、输出上限等），API Key 不放这里。

常用环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `MINI_AGENT_PORT` / `MINI_AGENT_HOST` | 监听端口 / 地址 | 8000 / 0.0.0.0 |
| `MINI_AGENT_DATA_DIR` | 数据目录 | `~/.mini-agent` |
| `MINI_AGENT_WORKSPACE` | 沙箱根 | 当前用户数据目录 |
| `MINI_AGENT_PYTHON` | 工具调用 Python 的解释器路径（`run_python`、MCP 服务端、`run_shell` 里的 `python` / `pip`） | 后端自身的解释器 |
| `MINI_AGENT_MAX_OUTPUT` | 单次工具输出上限 | 8000 |
| `MINI_AGENT_STREAM` / `MINI_AGENT_PARALLEL` | 流式 / 并行 | 1 / 1 |
| `MINI_AGENT_MAX_WORKERS` | 并行线程数 | 4 |
| `MINI_AGENT_MAX_TURNS` | 最大轮数 | 10 |
| `MINI_AGENT_ALLOWED_HOSTS` | 追加允许的 Host | 空 |
| `MINI_AGENT_ALLOW_DANGEROUS` | 放开危险命令拦截 | 0 |

旧名 `AGENT_*` 仍可读。

`MINI_AGENT_PYTHON` 的边界：

- 只影响**子进程**。`run_python`、MCP 服务端、`run_shell` 里的 `python` / `pip` 都按它解析（含 PATH 前置解释器目录）。
- 进程内工具（`python-docx`、`openpyxl`、`PIL` 等直接 import 的）仍用**后端解释器**。两者不一致时启动日志会记一条 `python.interpreter_mismatch`，提示两边依赖要各自装好。
- 建议写绝对路径。写相对路径时按后端进程的**当前工作目录**解析，用 `startup*.bat`（内部 `cd /d "%~dp0"`）启动时等于项目根；从其他目录启动会解析失败并回退后端解释器。
- 指向的文件不存在时不报错退出，只记一次 `python.executable_not_found` 并回退。

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
- 前端：`web/src/components/SettingsModal.tsx`、`web/src/theme.tsx`（主题状态与系统跟随）、`web/src/theme-dark.css`（深色调色板）、`web/src/main.tsx`（antd algorithm）、`web/index.html`（首帧主题脚本）
- 测试：`tests/test_logging.py`、`tests/test_webui.py`（通用配置用例）

## 注意事项

- 配置保存即生效；但工具模块与 MCP 配置的改动仍需重启后端。
- 「恢复默认」只重置通用配置，不动模型配置与账号。
- 主题是设备本地偏好：换浏览器、清站点数据、换设备都会回到「跟随系统」，这是预期行为；需要多设备一致时目前只能各自设置一次。
- 日志里没有对话正文，排障时结合事件流（运行轨迹）看模型实际收到了什么。
- 命令行参数优先级最高，适合临时覆盖，不要和持久配置混着排查。
