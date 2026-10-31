# 本地测试指南

> 目标：**一条命令建环境，一条命令跑全部测试**，而且不需要 API Key、不花钱。
> 本文只讲本地；CI/生产部署不在范围内。

---

## 0. 最短路径（TL;DR）

```bash
cd mini-agent

# ① 建虚拟环境 + 装依赖 + 自检（一步）
.venv\Scripts\activate            # Windows CMD（建完才有，见下）
```

Windows 上其实连激活都不用，两个批处理直接双击/运行即可：

```bat
scripts\setup_venv.bat        :: 建 .venv、装依赖、跑自检
scripts\run_tests.bat         :: 跑全部免费用例（24 个套件）
scripts\run_tests.bat --real  :: 额外跑真实 API 用例（消耗 token）
```

类 Unix（含 Git Bash）：

```bash
./scripts/setup_venv.sh
./scripts/run_tests.sh
./scripts/run_tests.sh --real
```

---

## 1. 为什么要用虚拟环境（这个项目里尤其重要）

不是「好习惯」这么虚的理由，这里有三条**具体**的：

1. **这个项目会固定一个模型 SDK 大版本。** `openai>=1.30.0` 是新的 client 形态
   （`client.chat.completions.create`）。装在全局环境里，将来别的项目需要 `openai<1`
   时会互相打架 —— 而 Agent 恰好是「会长期迭代」的项目，最容易踩这个。
2. **它会拉起子进程。** MCP server（`mcp/mcp_demo_server.py`）是作为 Python 子进程启动的。
   如果子进程落在**另一个**没装依赖的解释器里，握手会直接失败，
   报错信息还很不直观（超时，而不是 ImportError）。
3. **测试要能复现。** 七个套件里有真实的子进程、真实端口、真实计时，环境不一致时
   你分不清是代码坏了还是环境脏了。

第 2 条是本地测试最容易翻车的地方，第 3 节专门讲。

---

## 2. 建环境

### 2.1 用脚本（推荐）

```bat
:: Windows：在项目根目录双击或运行
scripts\setup_venv.bat

:: 指定 Python 版本（走 py 启动器）
scripts\setup_venv.bat 3.13
```

```bash
# 类 Unix / Git Bash
./scripts/setup_venv.sh
./scripts/setup_venv.sh python3.13
```

脚本做四件事：建 `.venv` → 升级 pip → 按 `requirements.txt` 装依赖 → 跑一次
工具加载自检（不需要 Key）。自检过了才算成功。

### 2.2 手工建（等价，照着敲更放心）

Windows CMD：

```bat
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Git Bash / macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt        # 类 Unix
.venv/Scripts/python.exe -m pip install -r requirements.txt # Git Bash（Windows 布局）
```

### 2.3 激活（可选但推荐）

```bat
.venv\Scripts\activate              :: CMD
.venv\Scripts\Activate.ps1          :: PowerShell
source .venv/Scripts/activate       :: Git Bash
source .venv/bin/activate           :: macOS / Linux
```

激活之后 `python` 就是 `.venv` 里的那个，可以直接 `python -m core`。
**不想激活也能用**：所有脚本内部都会自己找 `.venv`，显式写
`.venv\Scripts\python.exe -m core` 一样跑。

> 依赖里有一个**可选**项：`rich`（彩色表格）。
> 不装程序照常跑，会自动退回纯文本输出。所以「依赖装不全」不会让测试变红。

### 2.4 依赖清单

| 包 | 用途 | 不装会怎样 |
|---|---|---|
| `openai>=1.30.0` | 模型接入 | 跑不了任何真实调用；`tests/test_loop.py` 等假客户端用例仍可跑 |
| `rich>=13.0` | 终端彩色输出、表格、markdown 渲染（自检/脚本） | 自动退回 `PlainUI`，功能不变、观感朴素 |


---

## 3. 跑测试

### 3.0 目录速查（先看这个，路径就不会找错）

```
mini-agent/                     ← 项目根：所有相对路径的基准
├── src/core/                   ← 包本体（源码）
│   ├── _paths.py               ← ROOT 的定义处；路径问题的第一站
│   ├── agent.py  ui.py  loader.py  tools.py  main.py
│   └── registry / schema / sandbox / cancel / mcp 等
├── builtin_tools/  user_tools/     ← 工具目录（挂项目根，不在包内）
├── mcp/                        ← mcp_servers.json + mcp_demo_server.py
├── tests/                      ← 测试套件；bootstrap.py 是共用引导
├── scripts/                    ← setup_venv / run_tests / make_demo_case
├── docs/                       ← 你正在看的这份
├── logs/                       ← 真实运行日志
├── demo_case/                  ← 有标准答案的沙箱数据
└── pyproject.toml              ← src 布局 + mini-agent 命令
```

两条最容易搞混的规矩：

- **测试进程的 `cwd` 是项目根，但测试文件在 `tests/`。** 所以测试里引用
  `demo_case/inbox/x.csv` 这类相对路径是对的，而 `import agent` 靠的是
  `tests/bootstrap.py` 铺的 `sys.path`（把 `src/` 与 `src/core/` 都加上）。
- **`builtin_tools/` 在本轮重构后留在项目根，没有进包。** 它们是给你改的，
  放进包里 `pip install` 会把你的改动一起覆盖掉。

### 3.1 一条命令跑全套

```bat
scripts\run_tests.bat                     :: 24 个套件（免费用例）
scripts\run_tests.bat loop tools         :: 只跑名字含 loop / tools 的套件
scripts\run_tests.bat --list              :: 列出所有套件
scripts\run_tests.bat --fail-fast         :: 第一个失败就停
scripts\run_tests.bat --timeout 600       :: 单套件超时（默认 300s）
scripts\run_tests.bat --reinstall         :: 先同步依赖再跑
```

```bash
./scripts/run_tests.sh --list
./scripts/run_tests.sh cancel mcp_cancel
./scripts/run_tests.sh --real
```

实测输出（`.venv`，Windows）：

```
解释器：C:\...\mini-agent\.venv\Scripts\python.exe
工作区：C:\...\mini-agent
套件数：24（免费用例；加 --real 可包含真实 API 用例）
已跳过：tests/test_real_mcp_cancel.py
[1/24] tests/test_loop.py  —— 循环层 6 项：假客户端，不碰网络
          → PASS（12.4s）
...
  ✓ tests/test_loop.py                12.35s
  ✓ tests/test_tools.py             20.45s
  ✓ tests/test_stream_parallel.py     10.93s
  ✓ tests/test_cancel.py              13.14s
  ✓ tests/test_mcp_cancel.py          28.03s
  ✓ tests/test_http_e2e.py            27.89s

  24/24 通过 | 累计耗时约 4 分钟
全部通过 ✓
```

约四分钟（含系统监控套件的等待）。失败时脚本会把**该套件的完整输出**原样贴出来（不用往回翻屏），
并以退出码 1 结束（方便接 CI）。

### 3.2 单跑某个套件

```bat
.venv\Scripts\python.exe tests/test_cancel.py
```

| 套件 | 覆盖什么 | 耗时 |
|---|---|---|
| `tests/test_loop.py` | 循环层 6 项：假客户端，不碰网络 | ~12s |
| `tests/test_tools.py` | 工具层 + MCP 层 12 项 | ~20s |
| `tests/test_stream_parallel.py` | 流式聚合 + 并行执行 + MCP 并发 19 项 | ~11s |
| `tests/test_cancel.py` | 取消机制 11 项：令牌 / 杀进程 / 组隔离 / 流中断 / 消息补齐 / 原因送达 | ~13s |
| `tests/test_mcp_cancel.py` | MCP 取消通知 8 项：送达 / 停手 / 迟到响应 / 幂等 / 定向 | ~28s |
| `tests/test_http_e2e.py` | 本地 mock HTTP（支持 SSE）：流式三条降级路径 + 并行收益 | ~28s |

### 3.3 真实调用（消耗 token）

```bat
scripts\run_tests.bat --real
python tests/test_real_mcp_cancel.py                    :: 真模型 + 真 MCP：取消通知全链路
```

界面里的手动验证：启动 Web UI，在输入框发「分析 demo_case/inbox 目录，给出可复核的数字」。

需要先在 Web UI「设置 → 模型」里配置好模型（存 `~/.mini-agent/models.json`）。用 DeepSeek 的话，一次真实用例约几千 token。

> **为什么真实用例只留一个？** 它一次跑完「真模型决定调工具 → 工具执行中被取消 →
> 本端立刻停 → 通知真的发出去 → 服务端真的收手」这条完整链路，
> 拆成两个半覆盖的用例只会把同一批 token 花两遍。

---

## 4. 环境变量与 `.env`

`.env` 只放**系统配置**（监听地址、端口、路径等），模型与 API Key 不放这里 ——
它们由 Web UI「设置 → 模型」写入 `~/.mini-agent/models.json`。
`cp .env.example .env` 后按需改下面几行（都有默认值）：

```ini
MINI_AGENT_HOST=0.0.0.0      # 监听地址；127.0.0.1 = 仅本机
MINI_AGENT_PORT=8000
# MINI_AGENT_DATA_DIR=       # 数据目录（默认 ~/.mini-agent）
# MINI_AGENT_WORKSPACE=      # 沙箱根（默认当前用户数据目录）
# MINI_AGENT_ALLOWED_HOSTS=  # 追加允许的 Host（局域网域名）
```

其余运行时开关（同样有默认值，一般不用设；旧名 `AGENT_*` 仍可读）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `MINI_AGENT_WORKSPACE` | 沙箱根目录 | 当前用户数据目录 |
| `MINI_AGENT_MAX_OUTPUT` | 单次工具输出上限（字符） | 8000 |
| `MINI_AGENT_STREAM` | 流式输出 | 1（开） |
| `MINI_AGENT_PARALLEL` | 并行工具执行 | 1（开） |
| `MINI_AGENT_MAX_WORKERS` | 并行线程数 | 4 |
| `MINI_AGENT_MAX_TURNS` | 最大轮数 | 10 |
| `MINI_AGENT_ALLOW_DANGEROUS` | 放开危险命令拦截 | 0（拦） |

`run_tests.py` 会自动设 `MINI_AGENT_WORKSPACE`（指向项目根）与
`MINI_AGENT_PYTHON`（指向当前解释器），所以**从任何目录调用都不会踩路径坑**。

---

## 5. 四个容易踩的坑（都真的踩过）

### 5.1 解释器不一致 → MCP 握手超时，而不是 ImportError

`mcp/mcp_servers.json` 里写的是 `"command": "python"`。MCP 客户端只在 command
**恰好等于** `python` / `python3` 时才把它改写成 `sys.executable`：

```python
def _resolve_command(command: str) -> str:
    if command in ("python", "python3", "python.exe"):
        return sys.executable      # ← 用「拉起客户端的那个解释器」
    return command
```

这个设计的前提是：**跑 `main.py` 的解释器就是装了依赖的那个**。
如果 `python` 解析到系统 Python、而 `sys.executable` 是 venv 解释器，就出现
「主程序跑得动、MCP server 起不来」的诡异现象。两种解法：

```bat
:: ① 激活环境，让 python 就是 venv 里的
.venv\Scripts\activate

:: ② 永远显式用 venv 的解释器
.venv\Scripts\python.exe -m core
```

`run_tests.py` 已经用 `sys.executable` 拉起所有测试进程，所以这一条不会在测试里翻车。
若你自己的配置里想固定某个解释器，别写 `python`，写绝对路径 —— 那样
`_resolve_command` 会原样透传。

### 5.2 `py -m venv` 和 `python -m venv` 建出的环境可能不同

Windows 上 `py -3.13` 与 `python` 常常是两个解释器。测试里凡是出现
「X 里跑得好、Y 里跑不动」，先在两边各跑一次：

```bat
.venv\Scripts\python.exe -V
.venv\Scripts\python.exe -c "import openai, sys; print(openai.__version__, sys.executable)"
```

### 5.3 Git Bash 里的路径布局是 Windows 的

Git Bash 下 venv 的解释器在 `.venv/Scripts/python.exe`（不是 `bin/python`）。
`./scripts/run_tests.sh` 会两种布局都试，但你自己写命令时要注意：

```bash
.venv/Scripts/python.exe src/core/main.py       # Git Bash on Windows
.venv/bin/python -m core                        # macOS / Linux
```

### 5.4 批处理文件必须 CRLF + GBK

仓库里的两个 `.bat` 是 **CRLF 行尾**。如果被某个工具改成 LF，`cmd.exe` 的
`goto` / 标签会解析错位，表现为莫名其妙的
`'M' 不是内部或外部命令`。改完记得用 `git diff --stat` 看一眼行尾，
或者干脆只用 `.sh` / `run_tests.py`。

---

## 6. 边开发边测

```bat
:: 改完工具模块后重启后端（Web UI 不提供工具热重载）
startup_dev.bat

:: 在 Web UI「设置 → 系统配置 → 工具管理」查看已加载工具与来源
```

改工具之后最常用的验证组合：

```bat
.venv\Scripts\python.exe tests/test_tools.py      :: Schema / 注册表 / 加载器
.venv\Scripts\python.exe tests/test_cancel.py       :: 取消后消息序列是否仍合法
```

---

## 7. 目录里与测试相关的文件

```
mini-agent/
├── scripts\setup_venv.bat / .sh      ← 一键建环境（CRLF+GBK / POSIX）
├── scripts\run_tests.bat / .sh       ← 一键跑测试（薄封装，真正的逻辑在 run_tests.py）
├── run_tests.py              ← 测试编排：筛选 / 超时 / 汇总 / 环境准备
├── requirements.txt          ← 依赖（openai 必装，rich 可选）
├── .env.example              ← 复制为 .env 填 Key
├── test_*.py
├── demo_case/                ← 有标准答案的沙箱数据
└── mcp/mcp_servers.json          ← MCP server 配置（demo server 默认挂着）
```

`.venv/` 不要提交（`.gitignore` 已包含，建议再确认一次
`git status` 里没有它）。
