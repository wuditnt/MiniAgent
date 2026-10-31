# mini-agent

一个**可扩展的本地智能体 Web 应用**：核心循环 + 工具注册表 + 工具目录 + 连接（MCP）双通道。

核心循环始终只有约 250 行。加工具**不需要改核心代码** —— 这是整个设计的目标。

## 它是什么

- **本地优先**：文件、命令、模型 Key、会话数据都留在本机；可写数据统一放 `~/.mini-agent/`。
- **Web UI 唯一入口**：React + Ant Design X 聊天界面，流式输出、工具轨迹、确认弹窗、内联提问。
- **双模式**：同一个会话里逐条消息切换「快速对话」与「智能体」。
- **可扩展**：内置工具、用户工具、MCP 外部工具最终都是同一个 `Tool` 对象，进同一张注册表。

## 特性

- **智能体循环**：流式聚合、同轮工具并行执行、结果按序回填、上下文三级收敛、轮数用尽自动收尾。
- **三档确认**：按需询问 / 自主判断 / 完全控制；危险的 MCP 工具在任何档位都强制弹窗。
- **内联提问**：模型缺信息时直接在回复气泡里问你，答案在同一轮继续执行。
- **技能系统**：一个目录 + `SKILL.md` 就是一段工作流；启动只注入一行清单，命中后按需加载正文。
- **三层记忆**：用户画像、项目记忆、主题摘要；带分级、相关性筛选与保护区。
- **工作台**：只读浏览工作区，支持代码、Markdown、HTML/MHTML、图片、PDF、CSV 预览。
- **运行轨迹**：每次运行落盘为 append-only 事件流，可按 `run_id → turn` 回放并完整重建请求。
- **联网搜索**：DeepSeek / Tavily / 博查 / Bing 可切换，与聊天模型解耦，按会话开关。
- **子智能体**：只读子代理把调查类子任务隔离执行，只交回「结论 + 证据 + 未决」。
- **多模型与推理等级**：DeepSeek / 智谱 / Kimi / 通义 / 豆包 / OpenAI / Ollama 等，会话级选择 Low / High / Max。

## 技术栈

Python 3.10+（后端零新依赖，`ThreadingHTTPServer` + SSE）· React + TypeScript + Vite + Ant Design X · OpenAI 兼容接口 · MCP（stdio）· SQLite（账号库）。

## 快速开始

```bat
cd mini-agent

:: ① 建虚拟环境 + 装依赖 + 自检
scripts\setup_venv.bat          :: Windows CMD（双击也行）
```

```bash
./scripts/setup_venv.sh         # Git Bash / macOS / Linux
```

```bash
# ② 构建前端并启动（生产模式，运行时不需要 Node）
cd web; npm install; npm run build
cd ..; python -m core           # 浏览器打开 http://127.0.0.1:8000
```

首次启动自动创建超级管理员 `admin` / `admin123`，登录后强制改密。然后在「系统配置 → 模型配置」里选服务商、填 API Key、做连接测试。

开发模式（前端热更新，两个终端）：

```bash
python -m core --port 8123      # 终端 1：后端 API + SSE
cd web; npm run dev             # 终端 2：Vite :5173，/api 自动代理
```

常用开关：`--port` / `--host` / `--workspace` / `--data-dir` / `--no-login` / `--reset-password`。完整说明见 [快速入门](docs/01-quickstart.md)。

## 三条扩展路径

| 路径 | 怎么做 | 适合什么 |
|---|---|---|
| **工具文件** | 往 `user_tools/` 丢一个 `.py` | 自己写的能力，进程内执行 |
| **连接（MCP server）** | 在 `mcp/mcp_servers.json` 加一段配置 | 复用现成生态，独立进程 |
| **内置工具** | 改 `builtin_tools/*.py` | 项目自带的基础能力 |

```python
from core import tool

@tool
def now() -> str:
    """返回当前时间。"""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
```

还有第四种扩展：**技能**。往 `skills_user/` 丢一个含 `SKILL.md` 的目录，模型就能按你的工作流执行。

## 文档

| 文档 | 内容 |
|---|---|
| [文档索引](docs/README.md) | 三类阅读路径（新人 / 使用者 / AI 助手） |
| [快速入门](docs/01-quickstart.md) | 环境、启动、配置、数据目录 |
| [产品架构](docs/02-architecture.md) | 分层、核心循环、请求链路、模块地图 |
| [功能特性](docs/03-features/) | 17 篇：对话、会话、模型、联网、确认、提问、附件、技能、工具、MCP、子代理、记忆、沙箱、工作台、轨迹、登录、配置 |
| [本地测试](docs/04-operations/testing.md) | 一条命令建环境、一条命令跑测试 |
| [故障排查](docs/04-operations/troubleshooting.md) | 按现象定位 |
| [已知问题](docs/04-operations/known-issues.md) | 设计边界与生产化缺口 |

每篇功能文档统一为：用途 / 概念 / 使用方式 / **关键代码位置** / 注意事项。最后一节供接手者与 AI 助手直接定位源码。

## 目录结构

```text
mini-agent/
├── src/core/                 包本体
│   ├── agent.py              核心循环：messages → LLM(流式) → tool_calls → 并行 → 回填
│   ├── registry.py           Tool / ToolRegistry / @tool
│   ├── loader.py             工具发现、加载、故障隔离
│   ├── skills.py             技能发现、清单注入、use_skill
│   ├── sandbox.py            沙箱：双通道 / 三档读取 / 越界写检测 / 可取消进程
│   ├── approval.py           三档确认 + request_approval
│   ├── ask.py                内联提问 + ask_user
│   ├── memory.py             三层记忆与保护区
│   ├── events.py             事件流（可追溯运行）
│   ├── mcp.py                MCP stdio 客户端
│   ├── subagent.py           只读子代理
│   ├── webui.py              Web UI 服务（HTTP + SSE）
│   └── …                     模型 / 配置 / 日志 / 认证 / 工作区
├── builtin_tools/            自带工具（24 个模块）
├── user_tools/               你的工具
├── skills/                   自带技能（8 个）
├── skills_user/              你的技能
├── mcp/                      MCP 配置 + demo server
├── web/                      React 前端
├── tests/                    测试套件 + run_tests.py
├── demo_case/                有标准答案的沙箱数据
└── docs/                     本文档
```

只读资源留在项目根（`builtin_tools/`、`skills/`、`mcp/`、`web/dist`）；可写数据在 `~/.mini-agent/`。

## 测试

```bat
scripts\run_tests.bat          :: 全部免费用例，不需要 API Key
scripts\run_tests.bat --real   :: 额外跑真实 API 用例（消耗 token）
```

```bash
./scripts/run_tests.sh
```

覆盖核心循环、流式降级、并行、取消、MCP 取消通知、工具 / 技能、记忆、事件流、登录、工作台等。CI 在 Windows 跑 pytest、在 Ubuntu 跑前端构建。详见 [本地测试](docs/04-operations/testing.md)。

## 已知限制

当前是**可扩展的地基**，不是生产系统。主要缺口：权限规则引擎、跨运行追踪与成本归因、记忆语义检索、MCP 双向能力、后端语音、线程级取消。完整清单与沙箱边界见 [已知问题](docs/04-operations/known-issues.md)。

局域网默认走明文 HTTP，只建议在可信网络使用；`--no-login` 仅供本机调试。
