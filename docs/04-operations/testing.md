# 本地测试

## 用途

**一条命令建环境，一条命令跑全部测试**，不花 API 费用。本文只讲本地；CI 在生产仓库里由 `.github/workflows/ci.yml` 负责。

## 最短路径

```bat
scripts\setup_venv.bat        :: 建 .venv、装依赖、跑自检
scripts\run_tests.bat         :: 跑全部免费用例
scripts\run_tests.bat --real  :: 额外跑真实 API 用例（消耗 token）
```

```bash
./scripts/setup_venv.sh
./scripts/run_tests.sh
./scripts/run_tests.sh --real
```

## 为什么要用虚拟环境

这个项目会拉起 Python 子进程（MCP server）。如果子进程落在另一个没装依赖的解释器里，握手会失败，报错是**超时**而不是 ImportError，很难查。三条具体理由：

1. 项目固定 `openai>=1.30.0` 大版本，装全局会与其它项目冲突。
2. MCP server 用 `"command": "python"` 启动，客户端只在这种情况下改写成 `sys.executable`。
3. 测试有真实子进程、真实端口、真实计时，环境不一致时分不清是代码坏了还是环境脏了。

## 建环境

脚本做四件事：建 `.venv` → 升级 pip → 按 `requirements.txt` 装依赖 → 跑一次工具加载自检。自检过了才算成功。

```bat
scripts\setup_venv.bat 3.13
```

手工等价：

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

激活是可选的：所有脚本内部都会自己找 `.venv`。

## 跑测试

```bat
scripts\run_tests.bat                 :: 全部
scripts\run_tests.bat loop tools      :: 只跑名字含 loop / tools 的
scripts\run_tests.bat --list          :: 列出套件
scripts\run_tests.bat --fail-fast
scripts\run_tests.bat --timeout 600   :: 单套件超时，默认 300s
scripts\run_tests.bat --reinstall
```

```bash
./scripts/run_tests.sh --list
./scripts/run_tests.sh cancel mcp_cancel
```

单跑某个套件：

```bat
.venv\Scripts\python.exe tests\test_cancel.py
```

套件清单以 `--list` 为准，当前包括：

| 套件 | 覆盖 |
|---|---|
| `test_loop.py` | 循环层：假客户端，不碰网络 |
| `test_tools.py` | 工具层 + MCP 层 |
| `test_skills.py` | frontmatter / 加载 / 冲突 / 热重载 / use_skill / 截断 / 启停 / 消息解析 |
| `test_vision.py` | 图片输入：嗅探 / 附件通道 / 拒绝 / 循环集成 |
| `test_stream_parallel.py` | 流式聚合 + 并行 + MCP 并发 |
| `test_cancel.py` | 令牌 / 杀进程 / 组隔离 / 流中断 / 消息补齐 / 原因送达 |
| `test_mcp_cancel.py` | 取消通知：送达 / 停手 / 迟到响应 / 幂等 / 定向 |
| `test_http_e2e.py` | 本地 mock HTTP（SSE）：流式三条降级 + 并行收益 |
| `test_webui.py` | SSE 事件序列 / 双模式 / 停止 / 断连后台继续 / 危险确认 / 409 / 工作区 / 模型 / 搜索 / 配置 / 显式记忆 |
| `test_sys_monitor.py` | CPU / 内存 / 磁盘 / 网络 / 开机时长 / 端口占用 |
| `test_memory.py` | 存储 / 保护区 / 工具不可见 / prelude / shell / 注入 / 分级淘汰 / 提炼 / 检索合并 |
| `test_logging.py` | 按日目录 / 错误聚合 / 幂等重配 / 关闭 / 配置 / 清理 |
| `test_workbench.py` | 目录 / 安全 / 编码 / 图片 / HTML / MHTML / PDF / 下载 / 表格 |
| `test_skill_admin.py` | 列表 / 详情 / 启停 / 编辑（内置 403、重名拦截）/ 软删除 |
| `test_events.py` | 只追加 / 请求可重建 / 裁剪留痕 / 开关 / 兜底 |
| `test_model_config.py` | 厂家 spec / 三种参数风格 / 迁移 / 会话解析 |
| `test_web_search.py` | 后端选择 / DeepSeek Key 复用 / 请求与解析 |
| `test_net_tools.py` | 云端 / 本地分派 / 失败回退 / 协议校验 / Fetch 解析 |
| `test_ask.py` | 返回协议 / 选项规范化 / 工具契约 / 等待桥 |
| `test_auth.py` | 建库 / 默认超管 / scrypt / 会话 / 限流 / 超管保护 / 改密 / 写入节流 |
| `test_tool_search.py` | 档位矩阵 / 按需加载 / 无命中清单 / 下轮回可见 / 缓存命中 |
| `test_subagent.py` | 任务包隔离 / 只读子集 / 请求体一致 / 结果截断 / 取消传导 / 超时 / 失败降级 / 开关 |

真实用例（默认跳过，`--real` 才跑）：`test_real_mcp_cancel.py`，一次跑完「真模型调工具 → 中途取消 → 通知送达 → 服务端停手 → 序列合法」全链路。

## 四个真实踩过的坑

### 1. 解释器不一致 → MCP 握手超时

`mcp/mcp_servers.json` 写 `"command": "python"`。客户端只在 command 恰好等于 `python` / `python3` / `python.exe` 时改写为 `sys.executable`。解法：激活 venv，或永远显式用 `.venv\Scripts\python.exe`。想固定解释器就写绝对路径。

### 2. `py -m venv` 与 `python -m venv` 可能不同

Windows 上 `py -3.13` 与 `python` 常是两个解释器。出现「X 里跑得好、Y 里跑不动」时，两边各跑一次版本与 `import openai` 检查。

### 3. Git Bash 里是 Windows 路径布局

解释器在 `.venv/Scripts/python.exe`，不是 `.venv/bin/python`。脚本会两种布局都试，手写命令时注意。

### 4. 批处理必须 CRLF + GBK

`.bat` 被改成 LF 后，`cmd.exe` 的 `goto` / 标签会解析错位，表现为 `'M' 不是内部或外部命令`。改完用 `git diff --stat` 看一眼行尾。

## 环境变量

`run_tests.py` 会自动设 `MINI_AGENT_WORKSPACE`（项目根）与 `MINI_AGENT_PYTHON`（当前解释器），并让 `bootstrap.py` 把数据目录指向临时目录，因此**不会读写真实的 `~/.mini-agent`**。

`.env` 只放系统配置，模型与 Key 由 Web UI 写入 `models.json`：

```ini
MINI_AGENT_HOST=0.0.0.0
MINI_AGENT_PORT=8000
# MINI_AGENT_DATA_DIR=
# MINI_AGENT_WORKSPACE=
# MINI_AGENT_ALLOWED_HOSTS=
```

## 关键代码位置

- 编排入口：`run_tests.py`（筛选 / 超时 / 汇总 / 环境准备）
- 共用引导：`tests/bootstrap.py`（铺 `sys.path`、临时数据目录）
- 脚本：`scripts/setup_venv.bat` / `.sh`、`scripts/run_tests.bat` / `.sh`
- CI：`.github/workflows/ci.yml`（Windows `pytest tests -q`；Ubuntu `npm run build` 含 `tsc --noEmit`）

## 注意事项

- 测试进程 `cwd` 是项目根，但测试文件在 `tests/`；`import agent` 靠 `bootstrap.py`。
- `builtin_tools/` 与 `user_tools/` 在项目根，不是包内；测试引用 `demo_case/...` 相对路径是对的。
- 改完工具后跑 `test_tools.py` 与 `test_cancel.py` 是最常用的组合。
- 真实用例只保留一个：拆成多个半覆盖用例只会把同一批 token 花两遍。
