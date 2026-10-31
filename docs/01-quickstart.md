# 快速入门

## 用途

从零跑起 mini-agent：建环境、启动 Web UI、配置模型、登录、验证第一个任务。

## 前提

- Python 3.10+（推荐 3.13）
- Node.js 18+（仅前端构建需要；生产运行时不需要）
- Windows / macOS / Linux

## 安装

一条命令建虚拟环境、装依赖并自检：

```bat
scripts\setup_venv.bat          :: Windows CMD（双击也行）
```

```bash
./scripts/setup_venv.sh         # Git Bash / macOS / Linux
```

手工等价步骤：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt           # macOS / Linux
.venv\Scripts\python.exe -m pip install -e .                  # 可选：装出 mini-agent 命令
```

可选能力分组（定义见 `pyproject.toml`）：

```bash
pip install -e ".[docs]"        # 办公文档基础
pip install -e ".[docs-fast]"   # + Excel 加速 / PDF 生成 / docx→Markdown
pip install -e ".[dataframe]"   # + polars 大表格分析
pip install -e ".[ocr]"         # + 扫描件 OCR（约 170MB）
pip install -e ".[docs-all]"    # 以上一次装齐
```

## 启动

```bash
# 生产模式：Python 托管已构建的前端
cd web; npm install; npm run build    # 仅首次或前端变更后需要
python -m core                        # 浏览器打开 http://127.0.0.1:8000
```

开发模式（前端热更新，两个终端）：

```bash
python -m core --port 8123            # 终端 1：后端 API + SSE
cd web; npm run dev                   # 终端 2：Vite :5173，/api 自动代理
```

三种启动方式等价：`python -m core`（推荐）、`python src/core/main.py`、`mini-agent`（装了 console script 后）。项目只提供 Web UI，没有命令行对话。

常用开关：

```bash
--port 8123               # 换端口（默认 8000）
--host 127.0.0.1          # 仅本机可访问（默认 0.0.0.0）
--workspace PATH          # 沙箱根目录
--data-dir PATH           # 数据目录（默认 ~/.mini-agent）
--model NAME              # 覆盖模型
--no-login                # 关闭登录（仅本机调试）
--reset-password admin     # 终端救援：重置密码后退出
```

## 首次配置模型

1. 浏览器打开 Web UI，用默认超管 `admin` / `admin123` 登录。
2. 首次登录会强制改密。
3. 打开「系统配置 → 模型配置」，选一个服务商模板（DeepSeek / 智谱 / Kimi / 通义 / 豆包 / OpenAI / Ollama 等），填 API Key 与模型名。
4. 点「连接测试」确认可用。配置写入 `~/.mini-agent/models.json`。
5. 回到对话输入框，在模型菜单里按会话选择模型与推理等级。

> `.env` 只放系统配置（监听地址、端口、数据目录）。API Key 不放 `.env`。

## 第一次任务

在智能体模式下发一条需要动手的指令，例如：

```
分析 demo_case/inbox 目录，给出可复核的数字
```

观察：流式回答、工具执行轨迹、危险确认弹窗（默认「按需询问」档）、历史刷新后恢复。

## 数据放在哪

可写数据统一在 `~/.mini-agent/`（Windows 为 `C:\Users\<you>\.mini-agent`）：

```text
~/.mini-agent/
├── settings.json        通用配置
├── models.json          模型配置（含 API Key）
├── workspaces.json      工作区表
├── skills_state.json    技能启停状态
├── auth.db              账号库（SQLite）
├── sessions/            会话 JSON + 事件流
├── memory/              用户画像
├── logs/                运行日志（按日期分目录）
└── .migrated            老数据迁移标记
```

项目根只保留只读资源：`builtin_tools/`、`skills/`、`mcp/`、`web/dist`。

Agent 的工作区（沙箱根）默认是 `~/mini-agent`，与数据目录是两回事。

首次升级时，如果 `~/.mini-agent` 不存在而旧的 `<项目根>/web_data` 有数据，启动会自动复制过去。

## 常用路径与命令

| 我要… | 怎么做 |
|---|---|
| 跑全部免费测试 | `scripts\run_tests.bat` / `./scripts/run_tests.sh` |
| 真实 API 测试 | `scripts\run_tests.bat --real`（消耗 token） |
| 忘记密码 | `python -m core --reset-password admin` |
| 看后端日志 | `~/.mini-agent/logs/<日期>/app.log` 等 |
| 看后端健康 | `GET /api/health`（免登录） |
| 改工具 | 改 `builtin_tools/*.py` 或往 `user_tools/` 丢 `.py`，重启后端 |

## 关键代码位置

- 入口与参数解析：[`src/core/main.py`](../src/core/main.py)
- 根目录锚点：`src/core/_paths.py` 的 `ROOT`
- 包门面 / 导出：[`src/core/__init__.py`](../src/core/__init__.py)
- `python -m core`：`src/core/__main__.py`
- Web 服务入口：`src/core/webui.py` 的 `serve()` / class `WebServer`
- 工具加载：`src/core/loader.py` 的 `load_tools()`

## 注意事项

- **必须使用虚拟环境**。MCP server 作为 Python 子进程启动，解释器不一致会表现为握手超时，而不是 ImportError。
- 默认监听 `0.0.0.0`，同网段设备可访问；局域网走明文 HTTP，只建议在可信网络使用。
- 前端未构建时启动会没有界面：先 `cd web && npm run build`，或用开发模式。
- 更多环境与测试细节见 [04-operations/testing.md](04-operations/testing.md)。
