# 常见故障排查

## 用途

按现象定位问题，并告诉你去哪里看证据。排查顺序建议：**先看日志 → 再看运行轨迹 → 最后复现最小用例**。

## 启动与访问

### 端口被占用 / 启动失败

```bash
python -m core --port 8123
```

看终端输出与 `~/.mini-agent/logs/<日期>/app.log`。

### 浏览器空白、没有界面

生产模式需要先构建前端：

```bash
cd web; npm install; npm run build
```

或者用开发模式（`python -m core --port 8123` + `cd web; npm run dev`，Vite :5173）。

### 局域网设备访问不了

默认监听 `0.0.0.0`，但仍需检查防火墙；反向代理 / 自定义域名要设置 `MINI_AGENT_ALLOWED_HOSTS`。

## 登录

| 现象 | 原因与处理 |
|---|---|
| 所有 `/api/*` 返回 401 | 未登录或 Cookie 失效；重新登录 |
| 登录后大量接口 403 | 首次使用默认超管，必须先改密 |
| 返回 403 且提示 Host 不合法 | 用自定义域名 / 反向代理访问，需把域名加入 `MINI_AGENT_ALLOWED_HOSTS` |
| 连续登录失败被锁 | 5 次失败锁 5 分钟（按用户名 + IP）；等锁过期或换账号 |
| 忘记密码 | `python -m core --reset-password admin`（终端通道） |
| 技能管理入口消失 / 403 | 远程访问时仅本机可操作，这是设计 |

## 模型与回答

| 现象 | 原因与处理 |
|---|---|
| 连接测试 401 / 403 | Key 无效或无权限；与网络错误分开提示 |
| 请求报「不支持 thinking / reasoning_effort」 | 系统会自动摘参重试一次；仍失败就换兼容模式或降推理等级 |
| 上下文超长报错 | 会自动压缩历史后重试；反复出现就调小「上下文字符上限」或减少轮数 |
| 没有 token 统计 | 服务端不支持 `stream_options`，已自动降级；不影响回答 |
| 回答不流式 | 服务端忽略 `stream`，已退回普通模式；属于服务端行为 |
| 回答凭空消失 | 检查 `ui.llm_delta()` 是否真的渲染；`last_answer_streamed` 标志用于避免两边都以为对方打过 |
| 豆包没有 Max 档 | 豆包接口只认 low / high，界面隐藏 Max |

## 任务与工具

| 现象 | 原因与处理 |
|---|---|
| 发新消息返回 409 | 同一会话已有任务在后台跑；等它完成或点停止 |
| 关页面后任务还在跑 | 设计如此：执行归后端，关页面只停推送 |
| 点停止后提问卡片变灰 | 连接断开按「未回答」处理，Agent 会带着假设继续 |
| 工具没加载 | 看启动日志 / `app.log` 的加载报告；坏模块只记错误，不影响其它工具 |
| 工具返回 `[error] ...` | 这是给模型的错误文本，模型会自行换策略；看 `tool.log` 定位根因 |
| 工具输出被截断 | 单次上限默认 8000 字符，可在通用配置调大 |
| 并行时偶发超时 | 早期 MCP 公共队列缺陷已修复（每请求专属队列）；若仍出现，看 `test_stream_parallel.py` |

## MCP

| 现象 | 原因与处理 |
|---|---|
| 握手超时，而不是 ImportError | 解释器不一致：MCP server 子进程落在没装依赖的 Python 里；显式用 `.venv\Scripts\python.exe` 或写绝对路径 |
| server 没挂上 | 检查 `mcp_servers.json` 的 `enabled`、命令是否存在、启动日志 |
| demo 工具不可见 | `mcp_demo_server.py` 默认关闭，把 `enabled` 改成 `true` |
| 取消后服务端还在跑 | 协议只负责送达；服务端要实现「收到通知就停手」 |
| 改配置不生效 | MCP 不热重载，需重启后端 |

## 数据与日志

- **日志位置**：`~/.mini-agent/logs/<日期>/app.log`、`llm.log`、`tool.log`、`error.log`。
- **模型到底看到了什么**：打开该会话的「运行轨迹」（事件流），`llm_request` 有逐条消息的 sha256，正文在同轮来源事件里。
- **数据目录**：默认 `~/.mini-agent`；用 `--data-dir` 或 `MINI_AGENT_DATA_DIR` 指定。
- **老数据迁移**：首次启动若 `~/.mini-agent` 不存在而 `<项目根>/web_data` 有数据，会自动复制；原目录保留。
- **工作区不是数据目录**：Agent 的沙箱根默认是 `~/mini-agent`，与配置 / 会话存放位置不同。

## Windows

| 现象 | 处理 |
|---|---|
| `openai` 首次加载报 `DLL load failed: 句柄无效` | `agent.py` 已在进程早期 import `ctypes` 规避；仍出现就检查 Python / 依赖版本 |
| `.bat` 出现 `'M' 不是内部或外部命令` | 文件行尾被改成 LF；恢复 CRLF + GBK |
| Git Bash 找不到解释器 | 用 `.venv/Scripts/python.exe`，不是 `.venv/bin/python` |

## 关键代码位置

- 日志：`src/core/logging_setup.py`
- 健康检查：`src/core/webui.py` 的 `_handle_health()`（`GET /api/health` 免登录）
- 启动参数：`src/core/main.py`
- 根路径：`src/core/_paths.py`
- MCP 命令解析：`src/core/mcp.py` 的 `_resolve_command()`
- 流式降级与重试：`src/core/agent.py` 的 `_request_or_cancel()` / `_consume_stream_safe()`

## 注意事项

- 先看 `error.log`，再看 `llm.log` 与 `tool.log`，比在终端翻屏快。
- 不要把 API Key 贴进 issue；接口回显已脱敏，日志也不记录正文。
- 复现时尽量用 `run_tests.py` 跑对应单套件，能排除环境差异。
