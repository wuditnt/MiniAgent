# mini-agent

一个**可扩展**的本地智能体控制台：核心循环 + 工具注册表 + 插件目录 + MCP 双通道。

核心循环始终只有约 250 行。加工具**不需要改核心代码** —— 这是整个设计的唯一目标。

```
mini-agent/
├── src/core/                     ← 包本体（源码都在这里）
│   ├── __init__.py / __main__.py 包门面与 `python -m core` 入口
│   ├── _paths.py                 根目录锚点：全项目「根在哪」的唯一答案
│   ├── main.py                   入口 / CLI 参数 / .env 读取
│   ├── agent.py                  核心循环：messages → LLM(流式) → tool_calls →
│   │                             并行执行 → 按序回填 → …
│   ├── repl.py                   控制台：对话 / 斜杠命令 / 工具轨迹 / 危险确认
│   ├── ui.py                     呈现层：PlainUI / RichUI（流式增量、批次轨迹）
│   ├── loader.py                 插件发现 / 加载 / 热重载 / 故障隔离
│   ├── skills.py                 技能发现：SKILL.md / 清单注入 / use_skill
│   ├── tools.py                  向后兼容垫片（import tools 即加载插件）
│   ├── registry.py               ToolRegistry：唯一的工具存放处
│   ├── schema.py                 从类型注解 + docstring 自动推导 JSON Schema
│   ├── sandbox.py                工作区沙箱（读写双通道 / 三档读取 / 越界写检测）/ 截断
│   ├── approval.py               三档确认模式（ask/auto/full）+ request_approval 核心工具
│   ├── python_prelude.py         run_python 注入的写护栏（区外写/删/改一律拦截）
│   ├── cancel.py                 取消令牌 / 杀进程树 / 原因留痕
│   ├── mcp.py                    MCP stdio 客户端 + 桥接成 Tool（并发 / 取消通知）
│   └── images.py                 图片附件通道（view_image 的底层）
│
├── plugins/           自带插件（fs / exec / net / vision）
├── plugins_user/      ← 你的插件放这里，丢个 .py 就多一个工具
├── skills/            自带技能（一个目录 + SKILL.md = 一段可复用工作流）
├── skills_user/       ← 你的技能放这里，丢个含 SKILL.md 的目录就多一个技能
├── mcp/
│   ├── mcp_servers.json          ← 外部 MCP server 挂载配置
│   └── mcp_demo_server.py        验证协议链路的 demo server（6 个工具）
├── tests/                        测试套件 + 统一入口 run_tests.py
├── scripts/                      建环境 / 跑测试 / 造 demo 数据
├── docs/                         测试指南与历次专题报告
├── logs/                         真实运行日志
├── demo_case/                    有标准答案的沙箱数据
├── pyproject.toml                src 布局 + console script
└── requirements.txt
```

> **关于「根目录」**：`plugins/`、`mcp_servers.json`、`demo_case/` 都挂在**项目根**，
> 不是包内 —— 它们是你（用户）要改、要放东西的地方，不是只读资源。
> `src/core/_paths.py` 负责把这件事说清楚：`ROOT` 从环境变量
> `MINI_AGENT_ROOT` 或 `__file__` 上溯解析，所以**从任何目录启动都能找到它们**。

**三条扩展路径，最终都变成同一个 `Tool` 对象、进同一个注册表、出现在同一个 tools 数组里。**

| 路径       | 怎么做                         | 适合什么                                          |
| -------- | --------------------------- | --------------------------------------------- |
| **插件文件** | 往 `plugins_user/` 丢一个 `.py` | 你自己写的能力，进程内执行，最灵活                             |
| **MCP**  | 在 `mcp/mcp_servers.json` 里加一段配置 | 复用现成生态（filesystem / git / sqlite / 浏览器…），独立进程 |
| **内置插件** | 改 `plugins/*.py`            | 项目自带的基础能力                                     |

> **还有第四种扩展——技能（Skill）。** 上面三条路最终都变成 `Tool` 对象（模型能*调用*），
> 技能则是模型能*遵循*的**工作流**：往 `skills_user/` 丢一个含 `SKILL.md` 的目录。
> 启动时技能的一句话清单会注入 system prompt，模型命中后调用 `use_skill` 工具
> 把完整正文拉进上下文。详见「加一个技能」。

---

## 快速开始

```bash
cd mini-agent

# ① 建虚拟环境 + 装依赖 + 自检（一条命令）
scripts\setup_venv.bat          # Windows CMD（双击也行）
./scripts/setup_venv.sh         # Git Bash / macOS / Linux

# ② 填 API Key
cp .env.example .env    # 填 OPENAI_API_KEY（.env.example 里已预置 DeepSeek/通义/智谱/Ollama 地址）

# ③ 先跑免费用例，确认环境没问题（不需要 Key）
scripts\run_tests.bat           # Windows；类 Unix 用 ./scripts/run_tests.sh
```

手工建环境（等价）：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt           # macOS / Linux
.venv\Scripts\python.exe -m pip install -e .                  # 可选：装出 mini-agent 命令

python -m core --tools      # 先看看有哪些工具（不需要 Key）
python -m core --selftest   # 工具层自检（不需要 Key）
python -m core              # 进入控制台
```

> 三种启动方式等价：`python -m core`（推荐）、`python src/core/main.py`
> （直接跑源码，不用安装）、`mini-agent`（装了 console script 之后）。

> **务必用虚拟环境**，这不只是好习惯：MCP server 是当作 Python 子进程拉起来的，  
> 解释器不一致会让握手超时（且报错很不直观）。细节见  
> [docs/LOCAL\_TESTING.md](docs/LOCAL_TESTING.md) 第 1、5 节。

`.env` 里 DeepSeek / 通义 / 智谱 / 本地 Ollama 的地址都已预置，改一行即可切换。

---

## Web UI（浏览器界面）

除了控制台，还内置一个本地 Web UI（React + Ant Design X 聊天界面），支持
**对话 / 智能体双模式**（逐消息切换，历史保留）与**联网搜索开关**（内置
`web_search` 工具：Tavily / 博查 / Bing 兜底，按可用性三级自动选择）。

```powershell
# 生产模式（推荐，运行时无需 Node）
cd web; npm install; npm run build   # 仅首次或前端变更后需要
python -m core --web                 # Python 托管 web/dist
# 浏览器打开 c

# 开发模式（前端热更新，两个终端）
python -m core --web --port 8123     # 终端 1：后端 API + SSE
cd web; npm run dev                  # 终端 2：Vite :5173，/api 自动代理
```

要点：

- 默认只监听 `127.0.0.1`（`--host` 可改），数据不出本机；新增 `--web` / `--port`（默认 8000） / `--host` 开关
- chat 模式 = 纯对话（关联网时零工具、单次直答）；agent 模式 = 全量 80 个内置工具循环；开联网则追加 `web_search`
- 流式输出、工具执行轨迹（ThoughtChain，参数/结果可展开）、中途停止、刷新后恢复历史
- **三档确认模式**（仅 agent 模式，输入框工具栏盾牌胶囊切换，localStorage 记住选择）：
  - **按需询问（ask，默认）**：每个危险工具执行前都弹窗确认
  - **自主判断（auto）**：普通危险操作不再打扰，系统提示词注入责任准则，模型认为有风险时主动调 `request_approval(reason)` 向你申请（弹窗展示申请理由，批准才继续）；**危险的 MCP 工具在任何档位都强制弹窗**，这是硬特例
  - **完全控制（full）**：沙箱内操作一律不弹（MCP 工具仍弹）
  - 控制台 `/approve` 在 ask / full 两档间切换；命令行 `--approve` 默认进入 ask
- **附件上传**：输入区左侧加号菜单可上传图片 / 文件（多选）。图片走视觉模型（`view_image` 通道），文件内容注入消息；助手回答中引用 `.attachments/` 路径的图片会直接渲染显示
- **每轮消息下方工具栏**（常显）：复制按钮悬停展开「复制为 Markdown / 复制为纯文本」菜单；助手消息支持**重新生成**（回溯到对应提问重答）；用户消息支持**编辑重发**（原文回填输入框）与**删除此条及之后消息**；另有本轮 token 用量（悬停看输入/输出/模型调用/工具调用明细）与耗时。历史回放的消息没有用量与耗时，对应项自动隐藏
- **顶栏操作**：用量统计面板（消息数 / token 汇总 / 平均每轮 / 总耗时 / LLM 与工具调用次数 + 逐条明细）、清空当前会话、导出会话为 Markdown 文件下载
- **上下文水位**：输入区上方实时显示上下文占用百分比（绿 → 黄 → 红渐变预警），上限取「通用配置」的上下文字符上限
- **快捷键**：`Ctrl/⌘+N` 新建会话、`Ctrl/⌘+E` 导出 Markdown、`Ctrl/⌘+Shift+Delete` 清空会话、`Ctrl/⌘+/` 查看帮助
- **窄屏适配**：≤768px 时侧栏自动收起为浮层（汉堡按钮展开），顶栏与输入区自动紧凑布局
- 富渲染：GFM 表格、KaTeX 公式（`$...$`）、ECharts 图表（` ```echarts ` 代码块，柱/线/饼/散点）、内联 HTML（经白名单消毒）；配套技能 `skills/rich-render/` 教模型主动用图表达
- **工作区管理**：左侧边栏按工作区分组管理对话，可新建 / 改名 / 移除；每个工作区的 `path` 就是 Agent 的文件操作边界（沙箱根），会话归属哪个工作区就在哪个目录里干活。默认工作区锚在**当前登录用户的数据目录**（如 `C:\Users\<you>\mini-agent`，启动时自动创建，不再拿源码目录当工作区），不可删除；组内还有对话时不允许删工作区
  - 输入框上方左侧有工作区胶囊：新对话可直接切换（不选就落在默认工作区），已开始的对话只读显示 —— 换沙箱根会让「之前写的文件在哪」变得难以理解
- **系统配置**：右上角齿轮图标打开，内含「模型配置」「通用配置」「插件管理」三个页签（顶栏点模型名也能进，直接落在模型配置页）
  - 模型配置：任意 OpenAI 兼容接口 —— 内置 DeepSeek（默认）/ 智谱 / Kimi / 通义千问 / 豆包 / 千帆 / 硅基流动 / OpenAI / Ollama / vLLM 模板，可加多份「服务商 + 模型」配置一键切换；带连接测试（`GET /models` 探测，不耗 token，401/403 与网络错误分开报）。配置存 `web_data/models.json`，**API Key 只在本机，不上传第三方**，接口列表回显时脱敏为 `****后四位`
  - 通用配置：最大轮数、上下文字符上限、保留最近轮数、单次工具输出上限、温度、流式输出、并行工具、并发线程数，保存即生效（`web_data/settings.json`，可一键恢复默认）。取值优先级：命令行显式参数 > 这里保存的值 > 环境变量 > 内置默认
  - 通用配置 → **沙箱读取范围**（`sandbox_read`，默认 `strict`）：控制 Agent 读工作区**之外**文件的口径 —— `strict` 越界读写全拦；`whitelist` 仅白名单绝对路径可读（TextArea 每行一条）；`open` 沙箱外全部只读放开。**任何档位下，沙箱外的写入 / 删除 / 移动一律硬禁**，没有放开开关；此外 `run_python` 注入写护栏、`run_shell` 检测越界写命令、zip/tar 解压防路径穿越（混淆写法无法 100% 拦截，详见下文「沙箱」一节）
  - 插件管理：展示当前全部工具（名称 / 来源 / 分组 / 签名），按来源标记为 core / builtin / user / mcp，支持查看 dangerous / exclusive 标记与分组汇总，便于快速了解 Agent 当前具备的能力
- **多会话与搜索**：对话本地持久化（`web_data/sessions/*.json`，原子写），搜索支持标题 + 全文、空格分隔的多词 AND、命中片段高亮，点结果直接滚动定位到那条消息并闪烁
- **上下文记忆**：超长对话三级收敛 —— 先裁较早的工具结果、再把较早整轮滚动摘要成一段（保留任务目标/决策/进度/待办）、最后滑动窗口硬截断（保留 system + 摘要 + 最近 8 轮）。裁剪以「轮」为单位，`tool_calls` 与 `tool` 结果要么一起留要么一起删，不会配对错位导致接口报错
- 后端为 `src/core/webui.py`：stdlib `ThreadingHTTPServer` + SSE，Python 侧零新依赖；协议与设计见 [docs/WEBUI_ANTDX_PLAN.md](docs/WEBUI_ANTDX_PLAN.md)

---

## 加一个工具：三行

新建 `plugins_user/whoami.py`：

```python
from core import tool

@tool
def now() -> str:
    """返回当前时间。"""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
```

然后在控制台里敲 `/reload` —— 完事。没有注册表文件、没有装饰器参数、没有 JSON。

模型看到的 schema 是这样自动生成的：

```json
{"type": "object", "properties": {}, "required": []}
```

再复杂一点，参数也会自动推导：

```python
@tool
def count_lines(path: str, top: int = 5) -> str:
    """统计文件行数，并列出最长的前若干行。

    Args:
        path: 相对工作区的文件路径
        top: 列出最长的前 N 行
    """
```

→ 名字 `count_lines`，描述取自首行，`path` 自动必填（string），  
`top` 自动带 `default: 5`（integer），两个参数的说明从 `Args:` 段里抽取。

细节与进阶写法见 [`plugins_user/README.md`](plugins_user/README.md)。

---

## 加一个技能：一个目录

技能（Skill）和工具的区别：工具是模型能*调用*的能力（一次函数调用），
技能是模型能*遵循*的工作流（一段指令，通常会编排多个工具）。

新建 `skills_user/analyze_csv/SKILL.md`：

```markdown
---
name: analyze-csv
description: 分析 CSV 数据并产出可复核的数字结论
---

# CSV 数据分析流程

1. 先确认编码（`run_shell` 看魔数，或 `run_python` 试 decode）
2. 再用 `run_python` 读取并统计行数、列名、缺失值、数值分布
3. 结论必须附带证据（计数、聚合数字、抽样行号）
```

然后在控制台里敲 `/skills reload` —— 完事。没有注册表文件、没有装饰器。

**它是怎么生效的（清单 + 按需加载）：**

1. 启动时，所有技能的 `name: description` 变成一张清单，追加到 system prompt；
2. 模型判断任务命中某个技能时，调用内置的 `use_skill` 工具，
   把该技能的完整正文拉进上下文，再照着做。

这样技能写再长，也只在不相关的任务里占一行清单的位置，
几十个技能也不会吃掉上下文 —— 这是「清单 + 按需加载」相比「全部塞进
system prompt」的决定性差异。

frontmatter 的 `name` / `description` 都可省略：`name` 省略时用目录名，
`description` 省略时取正文首行。约定与进阶说明见
[`skills_user/README.md`](skills_user/README.md)。

---

## 挂一个 MCP server：一段配置

编辑 `mcp/mcp_servers.json`：

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

启动后，这个 server 的每个工具都会以 `filesystem__<工具名>` 出现在注册表里。  
控制台里 `/mcp` 看状态，`/mcp reload` 重新挂载。

仓库自带一个 `mcp/mcp_demo_server.py` 用于验证协议链路（`now` / `sha256` / `env_info` / `sleep` /  
`stubborn_sleep` / `cancel_log`），`mcp/mcp_servers.json` 里默认就挂着它。  
`sleep` 是刻意慢的工具，用来量并行的真实收益；后三个是取消通知专项测试用的  
（会收通知的、故意无视通知的、回显收到过什么通知的）。

协议侧支持：`initialize` 握手 → `tools/list` → `tools/call`，  
客户端的取消会以 `notifications/cancelled{requestId, reason}` 告知服务端  
（见「取消通知：别让对端白跑」）；服务端进程跨 run 复用，取消不会把它杀掉。

> **关于 `dangerous`**：外部工具默认按危险处理 —— MCP 工具执行前若 `/approve on` 会问你。  
> 明知安全的 server（比如只读的 filesystem）可以显式设 `"dangerous": false`。

---

## 流式输出与并行工具执行

这两件事看起来是"优化"，实际是**感知**和**性能**上收益最大的两处改造。

### 流式：把"等"变成"看"

开启后（默认开），回答逐字上屏。真正的工作量不在"接收"，而在**把分片拼回一条完整消息**：

```
分片到达：
  delta.content         → 直接累加
  delta.tool_calls[]    → 必须按 index 聚合：
                          id / name 只在第一片出现（重复覆盖会丢）
                          arguments 是逐片累加的（只取最后一片必然解析失败）
  usage                 → 藏在 choices 为空的「纯统计帧」里（漏了就没有 token 统计）
```

代价与取舍：

| 你得到                       | 你放弃                             |
| ------------------------- | ------------------------------- |
| 首字延迟从「整段生成完」降到「第一个 token」 | 流式文本是纯文本，不能再交给 rich 重排 markdown |

想要富文本排版就 `/stream off`，那时整段回答会交给 rich 渲染（代码块、表格更好看）。

三级降级（都是真实世界里会遇到的服务端行为）：

1. 不接受 `stream_options` → 去掉它重试，只是流式下没有 token 统计
2. 压根不支持 `stream` → 退回普通模式，功能不受影响
3. **答应了流式却一帧都不发**（网关/代理常见）→ 本轮自动改用普通模式重试。  
   之所以敢重试：一帧都没收到，既不会重复计费也不会丢内容

### 并行：同一轮的工具一起跑

模型经常在一轮里同时发好几个**互相独立**的调用。串行执行是纯浪费：

```
真实运行实测（DeepSeek，原始日志见 `logs/`）
  8 个工具并行完成，墙钟 0.61s（最长单个 0.60s）   加速 ≈ 7.9x
  2 个 run_python 并行，墙钟 3.89s（最长 3.89s）   加速 ≈ 2.0x
```

边界划得很清楚：

| 阶段                | 在哪个线程       | 为什么                      |
| ----------------- | ----------- | ------------------------ |
| 解析参数、查工具、**危险确认** | 主线程**串行**   | 确认要问人，不能并发弹窗             |
| 真正的工具执行           | 线程池并发       | 子进程 / IO / 网络能重叠         |
| 结果回填              | 主线程按**原顺序** | 协议要求 `tool_call_id` 一一对应 |

不并行的三种情况：只有一个调用（线程池是纯开销）、`/parallel off`、  
本批里出现了 `exclusive=True` 的工具（它会改共享状态，必须独占）。

```python
@tool(exclusive=True)     # 会切分支、改全局配置、写同一个文件的工具
def switch_branch(name: str) -> str: ...
```

**并行把一个真实的并发缺陷暴露出来了**：`src/core/mcp.py` 原来从公共队列取响应、  
id 不匹配就丢弃 —— 并发时线程之间会互相取走对方的响应，表现为**只在并发时出现的随机超时**。  
已改为「每个在途请求一个专属队列，响应按 id 精确投递」，`tests/test_stream_parallel.py` 里有回归用例。

---

## 真正的取消（Ctrl+C 掐掉一切）

没有取消机制的 Agent，Ctrl+C 只有两种下场：碰巧打断主线程正在等的那个点  
（子进程和工具线程继续在后台跑），或者根本打不断（线程池等待、子进程 wait）。  
这里的实现是四件事的组合，缺一不可：

```
1. 一个全局可见的开关          core/cancel.py :: CancellationToken
   （每次 run() 新建，设为「当前令牌」，工具随时可查）
2. 所有阻塞点轮询它            工具函数 / 流式分片间 / MCP 等待循环 / 轮次之间
3. 触发时主动强杀              整棵进程树（Windows 用 taskkill /T）+ 关闭 HTTP 客户端
4. 把取消告知对端与用户         MCP: notifications/cancelled；UI: ui.cancelled(reason)
```

第 4 条最容易被漏掉，因为漏了本地也「看起来正常」：只在自己这边停止等待，  
对端并不知道，那个慢工具会一直跑到底。对外部世界来说，取消从未发生过。

### 各层是怎么接入的

| 层                          | 做法                                                                                                             |
| -------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `run_shell` / `run_python` | 底层换成 `sandbox.run_process`：Popen + 0.15s 短轮询，取消 → 杀树 → 返回 `[cancelled]`                                        |
| 纯 Python 工具（你写的插件）         | `from core import current_token`，长循环里 `tok.check()` 或查 `tok.cancelled()` 优雅收尾                                  |
| MCP 工具                     | 等待响应的循环改为短轮询；取消时**先补发 `notifications/cancelled` 再抛 `Cancelled`**。server 进程本身不受影响（连接跨 run 复用），迟到的响应被静默丢弃，后续调用照常 |
| 流式响应                       | 每个分片之间都是检查点，取消时 `stream.close()` 断开连接（不再浪费下行流量）                                                                |
| 卡住的非流式请求                   | 令牌的 `on_cancel` 钩子关闭 HTTP 客户端，socket 读立刻报错 → 兜底转成 `Cancelled`                                                  |
| 控制台 / 一次性模式                | `Agent.run()` 在取消收尾时调 `ui.cancelled(reason)`；调用方用 `agent.last_cancel_notice_shown` 决定要不要再打一遍                   |

### 取消通知：别让对端白跑（MCP）

只在自己的等待循环里 `return` 是**单方面的**。对端不知道，会把那个慢请求当正常  
请求跑到底 —— 一个服务端 `sleep(60)` 的工具会白占 60 秒 CPU 与连接。所以取消  
必须补一张协议帧：

```json
{"jsonrpc":"2.0","method":"notifications/cancelled",
 "params":{"requestId":3,"reason":"真实场景：用户不耐烦，按下了 Ctrl+C；客户端已中止等待 tools/call 的响应"}}
```

实现要点（`src/core/mcp.py`，约 40 行）：

- **`requestId` 精确定位**：只取消那一个在途请求，并发的其它请求毫发无伤；
- **幂等**：同一 `requestId` 只发一次（重复通知只会打扰对端）；
- **超时也发**：超时同样属于「我不再等这个响应了」，否则服务端白跑；
- **通知不是请求**：不等回执，写失败也不抛异常 —— 管道都断了，通知已无意义；
- **迟到响应必须丢弃**：对端没来得及收手时响应还是会来。它既不能唤醒任何人，  
  更不能被当成「server 主动发来的消息」混进消息通道 —— 客户端记下已放弃的 id，  
  按 id 认领并静默丢弃（`client.stats["late_responses"]` 可观测）；
- **`reason` 一路留痕**：`CancellationToken.reason` → 协议帧 → 服务端日志 → UI 提示，  
  最后用户看到的是「为什么停了」而不是一句无信息量的「已取消」。

服务端侧同样要配合才成立（协议只负责送达，「停不停手」是服务端的实现）：  
`mcp_demo_server.py` 收到通知后置位一个事件，慢工具在循环里 `_check_cancelled()`  
自行退出，**且按规范不回响应**（回一条「已取消」也是错的：客户端不认这个 id 了）。

```
通知到达 ──► 服务端置位取消事件 ──► 工具在下一个检查点退出（<50ms）
                                        └─► 不回响应 ──► 客户端若仍收到则静默丢弃
```

### 两条容易被忽略的规矩

1. **取消后消息必须仍然合法。** OpenAI 协议要求 assistant 消息里每个 `tool_call`  
   都有对应的 tool 回复。取消往往发生在「工具执行到一半」，  
   `agent._patch_cancelled_replies()` 会把缺的补成  
   `[cancelled] 用户取消了本次任务` —— 模型下一轮能看到「哪些工具没跑完」，  
   而不是被 API 报错拒绝。
2. **KeyboardInterrupt 不向外抛。** `Agent.run()` 把它归一化成  
   `core.cancel.Cancelled`，调用方（REPL / 一次性模式）只需要接一种异常。  
   清理过程中的第二次 Ctrl+C 会正常穿透（强制退出的后门）。

工具作者的两行接入：

```python
from core import current_token

def my_tool(...):
    tok = current_token()          # 没有任务在跑时是 None，不取消也不报错
    while working:
        if tok is not None:
            tok.check()            # 已取消 → 抛 Cancelled → 注册表转成 [cancelled]
```

实测（`tests/test_cancel.py` / `tests/test_mcp_cancel.py` / `tests/test_real_mcp_cancel.py`）：

```
✓ run_shell(sleep 30) 在 1.8s 内被整棵树终止（取消命令 30s → 收场 1.8s）
✓ 并行批次取消：未启动的调用快速失败，3 个 tool_call 都补了回复
✓ MCP 慢调用 0.47s 内响应取消，通知 0.47s 内送达对端，对端随即停手且不回响应
✓ 对端无视通知照样回响应 → 迟到响应被静默丢弃，同连接后续调用正常
✓ 连接关闭时在途请求 0.01s 内报错（而不是干等到 30s 超时）
✓ 真实 DeepSeek + 真实 MCP server：取消 3.0s 收场（工具本要跑 8s），
  通知 reason 已转述给服务端日志，序列合法且可继续对话
```

---

## 控制台命令

| 命令                  | 作用                                                         |
| ------------------- | ---------------------------------------------------------- |
| `/help`             | 帮助                                                         |
| `/tools [关键词]`      | 列出工具，可按名字 / 描述 / **分组** / 来源过滤（如 `/tools fs`、`/tools mcp`） |
| `/plugins`          | 列出插件模块、工具数、`PLUGIN_INFO`                                   |
| `/reload [模块]`      | **热重载插件**，不传则全量重载（含新增/删除的插件文件）                             |
| `/skills [reload]`  | 列出技能 / 重新扫描技能目录                                                                |
| `/skill [名字]`       | 查看某个技能的完整指引                                                                    |
| `/mcp [reload]`     | MCP server 状态 / 重新挂载（并显示取消通知、超时、迟到响应计数）                    |
| `/model [名字]`       | 查看或切换模型                                                    |
| `/stream on\|off`   | 流式输出（逐字生成）                                                 |
| `/parallel on\|off` | 同一轮的多个工具是否并行执行                                             |
| `/workers [N]`      | 并行工具的最大线程数                                                 |
| `/usage`            | 本次会话的 token 与调用统计                                          |
| `/context`          | 上下文占用与已裁剪条数                                                |
| `/verbose on\|off`  | 是否显示工具调用轨迹                                                 |
| `/approve on\|off`  | 切换确认模式：`on`=按需询问（ask）/ `off`=完全控制（full）；无参数在两档间切换 |
| `/reset`            | 清空对话历史                                                     |
| `/exit`             | 退出                                                         |

`/reload` 是开发体验的关键：把「改 → 退出 → 重启 → 重连 → 复现」压缩成「改 → /reload」。

---

## 命令行开关

```bash
python -m core "任务"          # 一次性执行
python -m core                 # 控制台
python -m core --tools         # 列工具
python -m core --plugins       # 列插件
python -m core --skills        # 列技能
python -m core --mcp-status    # 查 MCP
python -m core --selftest      # 自检

--model NAME          覆盖模型
--workspace PATH      工作区（沙箱根目录）
--plugin-dir A,B      替换插件目录
--skill-dir A,B       替换技能目录
--no-plugins          不加载插件
--no-skills           不加载技能
--no-mcp              不挂 MCP
--no-stream           关闭流式输出
--no-parallel         关闭并行工具执行
--workers N           并行工具线程数（默认 4）
--approve             危险工具需确认
--plain               不用彩色
-q / --quiet          不显示工具轨迹
```

对应环境变量：`AGENT_STREAM` / `AGENT_PARALLEL` / `AGENT_MAX_WORKERS`（默认 `1` / `1` / `4`，  
写成 `0` 即关闭）。

---

## 本地测试

**十一个套件，全部不需要 API Key、不花任何费用，约两分钟跑完。**  
完整指南（虚拟环境、四个真实踩坑、环境变量）见 [docs/LOCAL\_TESTING.md](docs/LOCAL_TESTING.md)。

```bash
# 一条命令跑全套（自动定位 .venv 里的解释器）
scripts\run_tests.bat          # Windows CMD
./scripts/run_tests.sh         # Git Bash / macOS / Linux

scripts\run_tests.bat --list           # 列出所有套件
scripts\run_tests.bat cancel mcp       # 只跑名字含 cancel / mcp 的
scripts\run_tests.bat --reinstall      # 先同步依赖再跑
scripts\run_tests.bat --real           # 额外跑真实 API 用例（消耗 token）
```


单个套件也可以直接跑（从项目根执行，或让 `run_tests.py` 代劳）：

```bash
.venv\Scripts\python.exe tests\test_loop.py      # 循环层 6 项：用假客户端，不碰网络
.venv\Scripts\python.exe tests\test_plugins.py   # 插件层 + MCP 层 11 项
```

| 套件 | 覆盖什么 |
|---|---|
| `tests/test_loop.py` | 循环层 6 项：假客户端，不碰网络 |
| `tests/test_plugins.py` | 插件层 + MCP 层 11 项 |
| `tests/test_skills.py` | 技能层 5 项：frontmatter / 加载 / 冲突 / 热重载 / use_skill |
| `tests/test_vision.py` | 图片输入 5 项：嗅探 / 附件通道 / 拒绝 / 循环集成 |
| `tests/test_stream_parallel.py` | 流式聚合 + 并行执行 + MCP 并发 19 项 |
| `tests/test_cancel.py` | 取消机制 10 项：令牌 / 杀进程 / 流中断 / 消息补齐 / 原因送达 |
| `tests/test_mcp_cancel.py` | MCP 取消通知 8 项：送达 / 停手 / 迟到响应 / 幂等 / 定向 |
| `tests/test_http_e2e.py` | 本地 mock HTTP（支持 SSE）：流式三条降级路径 + 并行收益 |
| `tests/test_webui.py` | Web UI 端到端 12 项：SSE 事件序列 / 双模式工具表 / 停止 / 危险确认 / 409 / 工作区 / 模型配置 / 会话搜索 / 通用配置 |
| `tests/test_sys_monitor.py` | 系统监控插件 8 项：CPU / 内存 / 磁盘 / 网络 / 开机时长 / 端口占用 |

实测（`.venv` 内，Windows）：

```
  ✓ test_loop.py                12.35s
  ✓ test_plugins.py             20.45s
  ✓ test_stream_parallel.py     10.93s
  ✓ test_cancel.py              13.14s
  ✓ test_mcp_cancel.py          28.03s
  ✓ test_http_e2e.py            27.89s

  6/6 通过 | 累计耗时 112.8s
```

`tests/test_http_e2e.py` 会用同一个 mock 服务跑四遍，专门验证流式的三条降级路径
（正常 SSE / 不认识 `stream_options` / 网关忽略 `stream`），最后量一次
「4 个 500ms 的 MCP 调用：串行 vs 并行」：

```
流式+并行              通过 ✓
stream_options 降级    通过 ✓
空流降级               通过 ✓
全同步                 通过 ✓
4 个 sleep(500ms) 的 MCP 工具调用：串行 2.00s → 并行 0.50s（加速 4.0x）
```

### 虚拟环境

```bash
scripts\setup_venv.bat          # Windows：建 .venv + 装依赖 + 自检（双击也行）
./scripts/setup_venv.sh         # Git Bash / macOS / Linux

.venv\Scripts\activate          # 激活（CMD）
source .venv/bin/activate       # 激活（macOS / Linux）
```

不激活也能用 —— `setup_venv.*` 与 `run_tests.*` 都会自己找 `.venv`。
**为什么必须用虚拟环境**：`mcp/mcp_servers.json` 里写的是 `"command": "python"`，
MCP 客户端只在它恰好等于 `python` / `python3` 时才改写成 `sys.executable`；
解释器不一致会让 **MCP server 子进程落在一个没装依赖的环境里**，
现象是握手超时而不是 ImportError，很难查。详见
[docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md) 第 5.1 节。

真实调用（会消耗 token，需要 `.env` 里的 Key）：

```bash
python -m core "分析 demo_case/inbox 目录，给出可复核的数字"
python -m core --no-stream --no-parallel "…"   # 对照：传统形态
python tests/test_real_mcp_cancel.py                 # 真实模型 + 真实 MCP server：取消通知全链路
```

---

## 目录结构

```
mini-agent/
├── src/core/           包本体
│   ├── _paths.py             根目录锚点：ROOT / DEFAULT_WORKSPACE / 铺 sys.path
│   ├── agent.py              核心循环：流式接收 / 并行执行 / 上下文裁剪
│   ├── repl.py               控制台：斜杠命令
│   ├── ui.py                 呈现层：PlainUI / RichUI（llm_delta / tools_start / cancelled）
│   ├── loader.py             插件加载器：发现 / 加载 / 热重载 / 故障隔离
│   ├── skills.py             技能加载器：发现 SKILL.md / 清单注入 / use_skill 工具
│   ├── tools.py              向后兼容垫片（老代码 import tools 仍可用）
│   ├── main.py               入口 / CLI / .env
│   ├── __main__.py           python -m core
│   ├── __init__.py           铺 sys.path + 导出插件作者要用的 API
│   ├── registry.py           Tool / ToolRegistry / @tool
│   ├── schema.py             自动 Schema 推导
│   ├── sandbox.py            沙箱与安全策略：safe_path/read_path 双通道、三档读取、
│   │                          shell 越界写检测、可取消的 run_process
│   ├── approval.py           三档确认模式（ask/auto/full）+ request_approval 工具
│   ├── python_prelude.py     run_python 写护栏（open/shutil/pathlib/sqlite 等 patch）
│   ├── cancel.py             取消令牌：CancellationToken / Cancelled / 杀进程树
│   ├── mcp.py                MCP stdio 客户端（并发安全 + 可取消 + 取消通知）+ 桥接
│   └── images.py             图片附件通道（view_image 的底层）
├── plugins/                  自带插件
│   ├── fs_tools.py           read_file / write_file / list_dir / glob_files / grep
│   ├── code_tools.py         edit_file（唯一匹配块编辑）/ re_replace / search_symbol /
│   │                          py_syntax_check / count_code_lines
│   ├── exec_tools.py         run_shell / run_python
│   ├── net_tools.py          fetch_url
│   └── vision_tools.py       view_image（图片输入：按魔数嗅探格式，附件进对话）
├── plugins_user/             你的插件（含示例 text_tools.py 与开发指南）
├── skills/                   自带技能（csv-analysis 示例）
├── skills_user/              你的技能（含约定说明，一个目录 + SKILL.md）
├── mcp/
│   ├── mcp_servers.json      MCP 配置
│   └── mcp_demo_server.py    自带 MCP server（now / sha256 / env_info / sleep /
│                             stubborn_sleep / cancel_log，后两个服务于取消通知测试）
├── tests/                    十一个套件 + bootstrap.py（共用引导）+ run_tests.py
├── scripts/                  setup_venv.* / run_tests.* / make_demo_case.py
├── docs/                     测试指南与历次专题报告
├── logs/                     真实运行日志
├── demo_case/                沙箱测试数据（有标准答案）
└── pyproject.toml            src 布局 + console script
```

---

## 几个设计决定，以及为什么

**1. 为什么工具要统一成 `Tool` 对象？**
因为一旦「内置 / 插件 / MCP」在核心循环里长得一样，加通道就不需要改循环。
`agent.py` 从头到尾没出现过 `isinstance` 或来源判断。

**2. 为什么把沙箱独立成模块？**
工具会被不断扩展，但边界必须只有一处定义。沙箱提供两个通道，插件作者按语义二选一：
`from core import safe_path, read_path`

- `safe_path(p)` —— **写通道**（写 / 删 / 移动 / 编辑 / 解压落地点）：目标必须在工作区内，
  区外一律 `PermissionError`，没有任何模式可以放开；
- `read_path(p)` —— **读通道**：区内放行；区外按 `sandbox_read` 三档判定
  （`strict` 拦 / `whitelist` 查表 / `open` 放行）。区外 SQLite 库自动改 `mode=ro` 只读连接，
  且只允许 SELECT/WITH/PRAGMA/EXPLAIN。

只把工具入口的路径换成 `read_path` 还不够 —— Agent 能写代码，写操作有三条旁路，各封一层：

1. **`run_python` 注入 prelude 写护栏**（`python_prelude.py`）：用户代码执行前包一层
   `_install()`，patch 掉 `open`/`os.remove`/`shutil.*`/`pathlib`/`sqlite3.connect` 等写入口，
   区外写直接抛 `PermissionError`；护栏一次性安装后摘除自身，不污染用户命名空间；
2. **`run_shell` 越界写检测**：重定向（`>`/`>>`）与写动词（copy/move/del/mkdir/…）
   的目标路径落在区外即拒绝执行；
3. **zip / tar 解压防穿越（zip-slip）**：逐成员 `realpath` 校验必须落在解压目录内，
   tar 额外拒绝符号链接成员。

已知边界：shell 与 Python 都是完整语言，变量拼接、编码、别名等**混淆写法无法 100% 拦截**，
所以「区外硬禁写入」的可靠底座仍是文件系统层 —— 工具通道、prelude、命令检测是纵深防御，
不是绝对边界。读取三档同理：`open` 只降低读门槛，写门槛永远不变。

**3. 为什么坏插件不中断启动？**
加载报告会把失败记成 `{模块: 错误}`，其它插件照常加载。可扩展系统的头号杀手是
"加了个坏东西，整个程序起不来"，而不是"某个工具不好用"。

**4. 为什么异常要转成文本？**
工具报错时把 `[error] ...` 当作工具结果还给模型，它下一轮会自己换策略。
在真实任务里这一点被验证过：`run_shell` 返回 `exit=1` 后，模型改用了 `run_python`，
整个任务没有中断。

**5. 为什么 `dangerous` 要单独标注？**
黑名单只能拦已知模式；让用户对危险操作做决定，是比黑名单更可靠的防线。
但「每个危险操作都问」在真实使用里疲劳且打断任务，所以确认做成三档（`approval.py`）：
ask 每个危险工具都问；auto 把判断权部分交给模型 —— 系统提示词注入责任准则，
由模型在做高风险动作前主动调 `request_approval(reason)` 申请；full 沙箱内不问。
两条硬底线不变：**任何档位下 dangerous 的 MCP 工具都弹窗**（外部能力不可信），
以及沙箱外不可写（见第 2 条）。三档与三读档正交，分别回答「要不要问」与「能不能读」。

**6. 为什么 `registry or REGISTRY` 是错的？**
这是个真实的坑（开发时就踩了）：`ToolRegistry` 定义了 `__len__`，
**空注册表是 falsy**，这行代码会把空注册表静默换成全局注册表，
表现为"工具注册成功但注册错了地方"。正确写法是 `registry if registry is not None else REGISTRY`。
`tests/test_plugins.py` 里有对应的回归用例。

**7. 为什么判断"是不是流"用的是 `__next__` 而不是 `__iter__`？**
`openai` 的 `ChatCompletion` 是 pydantic 模型，它**也**实现了 `__iter__`。
用 `__iter__` 判断会把普通响应误当成流去迭代。可靠的区别是：
**流是迭代器（有 `__next__`），响应对象不是。**
（这类"能跑但判断依据不牢"的写法，只有在换模型/换服务端时才炸。）

**8. 流式为什么要问 UI "你刚才真的显示了吗"？**
因为"显示过了"这个事实只有 UI 知道。如果 UI 被 `/verbose off` 关掉、
流式内容一个字都没打，Agent 却仍然告诉控制台"已经打过了"，
最终回答就会**凭空消失**（两边都以为对方打了）。
所以 `ui.llm_delta()` 返回布尔值，Agent 据此设置 `last_answer_streamed`。

**9. 并行执行时谁来打印轨迹？**
主线程。工作线程只负责跑工具并返回 `(结果, 耗时)`，
拿到结果后的打印全部回到主线程做 —— 这样轨迹行不会互相插队，
UI 也不被迫为并发加锁（`ui.py` 里仍备了一把可重入锁，作为纵深防御）。

**10. 为什么取消令牌是模块级变量而不是线程局部？**
一次只有一个 `Agent.run()` 在跑（REPL 是串行对话模型），
但**工具运行在工作线程里** —— 线程局部反而会让它们读不到令牌。
模块级 + 进出配对恢复（`set_current_token` 返回旧值）最简单也最正确。

**11. 为什么子进程要用短轮询而不是 `subprocess.run()`？**
`run()` 会把线程**死死阻塞**在内部 wait 上：Ctrl+C 打不断（尤其 Windows），
取消令牌也插不进去。`run_process` 每 0.15s 醒一次，既能查令牌又能杀树；
附带的好处是每次醒来 Python 都有机会处理信号，主线程场景下 Ctrl+C 也能及时送达。

---

## 生产化还差什么

当前是**可扩展的地基**，不是生产系统。要上线还需要：

- **持久化记忆**：跨会话的长期记忆与检索
- **权限策略引擎**：按工具/参数做细粒度规则，而不只是 yes/no
- **可观测性**：结构化日志、调用链、成本归因
- **MCP 高级能力**：resources / prompts / sampling / 进度通知
- **多模态**：图片输入已支持（`view_image` 工具 + [core/images.py](src/core/images.py)
  附件通道，模型须为视觉模型）；语音输入未做
- **上下文压缩**：现在只做"超限裁剪"，还没有摘要式压缩
- **取消的深化**：纯 Python 阻塞调用的线程强杀（Python 无法安全杀线程，只能靠工具
  作者自觉检查令牌）；服务端发的 `notifications/cancelled`（取消**它**发起的采样请求，
  目前客户端还不处理 server→client 请求）。MCP 的 `notifications/cancelled`（客户端→
  服务端）已实现，见「取消通知：别让对端白跑」。
