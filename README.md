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
│   ├── ask.py                    内联提问：ask_user 核心工具 + 询问通道（UI 卡片回传答案）
│   ├── python_prelude.py         run_python 注入的写护栏（区外写/删/改一律拦截）
│   ├── memory.py                 记忆系统：三层存储 / 保护区判定 / 注入渲染 / remember
│   ├── cancel.py                 取消令牌 / 杀进程树 / 原因留痕
│   ├── mcp.py                    MCP stdio 客户端 + 桥接成 Tool（并发 / 取消通知）
│   ├── webui.py                  Web UI 服务（ThreadingHTTPServer + SSE）
│   ├── workspace_config.py       工作区注册表与路径解析（沙箱根）
│   ├── model_config.py           模型配置：多服务商配置读写与连接测试
│   ├── settings.py               通用配置：settings.json 读写与应用
│   ├── logging_setup.py          文件日志：按日目录分文件 / 保留清理
│   ├── mhtml.py                  MHTML 解析：主 HTML + 内嵌资源（data: URL）
│   └── images.py                 图片附件通道（view_image 的底层）
│
├── plugins/           自带插件（文件 / 代码 / 执行 / 网络 / 视觉 / 系统 / 数据 / 文档 / 表格 / OCR 等，共 24 个）
├── plugins_user/      ← 你的插件放这里，丢个 .py 就多一个工具
├── skills/            自带技能（一个目录 + SKILL.md = 一段可复用工作流）
├── skills_user/       ← 你的技能放这里，丢个含 SKILL.md 的目录就多一个技能
├── mcp/
│   ├── mcp_servers.json          ← 外部 MCP server 挂载配置
│   └── mcp_demo_server.py        验证协议链路的 demo server（6 个工具）
├── tests/                        测试套件 + 统一入口 run_tests.py
├── scripts/                      建环境 / 跑测试 / 造 demo 数据
├── docs/                         测试指南与历次专题报告
├── demo_case/                    有标准答案的沙箱数据
├── pyproject.toml                src 布局 + console script
└── requirements.txt
```

> **关于「根目录」**：`plugins/`、`mcp_servers.json`、`demo_case/` 都挂在**项目根**，
> 不是包内 —— 它们是你（用户）要改、要放东西的地方，不是只读资源。
> `src/core/_paths.py` 负责把这件事说清楚：`ROOT` 从环境变量
> `MINI_AGENT_ROOT` 或 `__file__` 上溯解析，所以**从任何目录启动都能找到它们**。

---

## 数据目录

所有**可写**数据都放在 `~/.mini-agent/`（Windows 为 `C:\Users\<you>\.mini-agent`），
不再写进项目目录 —— 这样换机器、重新 clone、打包成桌面应用都不影响已有数据：

```text
~/.mini-agent/
├── settings.json        通用配置
├── models.json          模型配置（含 API Key）
├── workspaces.json      工作区表
├── skills_state.json    技能启停状态（只记停用的）
├── auth.db              账号库（SQLite，含 -wal / -shm）
├── sessions/            会话 JSON + 事件流
├── memory/              用户画像
├── logs/                运行日志（按日期分目录）
└── .migrated            老数据迁移标记（存在即不再重复迁移）
```

- 项目根只保留**只读资源**：`plugins/`、`skills/`、`mcp/`、`web/dist`
- 自定义位置：`--data-dir PATH`，或环境变量 `MINI_AGENT_DATA_DIR`
- **首次升级自动迁移**：若 `~/.mini-agent` 还不存在，而旧的 `<项目根>/web_data` 有数据，
  启动时会**复制**过去并打印提示；原目录保留，确认无误后可自行删除
- Agent 的工作区（沙箱根）仍是 `~/mini-agent`，与数据目录是两个不同的位置，不要混用

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

# 只想装部分能力时用 extra 分组（定义见 pyproject.toml）：
#   pip install -e ".[docs]"        # 办公文档基础：docx / xlsx / pdf / csv 增强
#   pip install -e ".[docs-fast]"   # + calamine 加速 Excel（含 .xls）、reportlab 生成 PDF、mammoth+markdownify docx→Markdown
#   pip install -e ".[dataframe]"   # + polars 大表格分析
#   pip install -e ".[ocr]"         # + 扫描件 OCR（约 170MB，可选）
#   pip install -e ".[docs-all]"    # 以上四组一次装齐

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
**对话 / 智能体双模式**（逐消息切换，历史保留）与**会话级「智能搜索」开关**（内置
`web_search` 工具，后端可在「系统配置 → 联网搜索」里选择：DeepSeek 官方 Search
API 默认、Tavily / 博查 / Bing 兜底；联网在两种模式下都可用 —— 快速对话只是
不做推理思考；开关按对话独立保存，互不影响）。

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

- 默认监听 `0.0.0.0`（本机 + 局域网均可访问，手机/同网段设备直连；传 `--host 127.0.0.1` 可收窄为仅本机）；新增 `--web` / `--port`（默认 8000） / `--host`（默认 `0.0.0.0`）开关
- chat 模式 = 快速对话（**不做推理思考，只保留联网能力**：开关打开时 `web_search` + `fetch_url`，关闭时只有本地 `fetch_url`）；agent 模式 = 全量内置工具循环（当前 94 个，含 `remember`；`request_approval` 仅 auto 档暴露），开联网时额外加载 `web_search`
- **联网的两套实现**（由顶栏「⋯」菜单第一项的**智能搜索**开关逐轮决定，文字 + 开关样式；配置**按会话独立保存**，切到别的对话互不影响）：
  - **开**：`web_search` 走所选云端后端；`fetch_url` 优先走 **DeepSeek Fetch** `POST https://api.deepseek.com/v1/tools/fetch`（返回干净 Markdown，与 Chat 同一把 Key，按次计费），失败自动回退本地抓取
  - **关**：不提供搜索；`fetch_url` 只做本地直连抓取（免费，剥离 HTML 标签），适合读取你明确给出的链接
  - 搜索后端（系统配置 → 联网搜索，`search_provider`）：与聊天模型解耦，**任何厂商的模型都能用所选搜索能力** —— `deepseek`（默认）调 DeepSeek 官方 Search API、`tavily` / `bocha` 分别读 `TAVILY_API_KEY` / `BOCHA_API_KEY`、`bing` 抓结果页。改完即时生效（无缓存）
- **会话级模型配置 + 推理等级**（输入框工具栏的模型菜单）：每个对话单独选模型与推理等级，**随会话 JSON 持久化、互不影响**（新会话默认使用模型列表里的第一份配置；换模型后下一次对话生效）。推理等级统一三档 **Low / High / Max**（默认 **High**），按厂家接口能力通用化适配 —— DeepSeek / Kimi / 千帆 / 硅基流动 / Ollama 等走 `reasoning_effort` 直传三档；智谱 GLM / 豆包走 `thinking` 开关 + `reasoning_effort`（**豆包接口只认 Low / High 两档**，Max 不显示）；通义千问兼容模式走 `enable_thinking` + `reasoning_effort`；OpenAI 无 `max` 档，界面三档重映射为 low/medium/high。未声明厂家按通用三档下发，接口不认思考参数时自动摘参重试一次。旧配置里的 `off` / `medium` 自动迁移为 `low` / `high`。**快速对话模式显式关闭思考**（`reasoning_effort: "none"`；thinking / switch 风格分别发 `thinking.type=disabled` / `enable_thinking=false`，不发参数并不等于不思考），推理等级仅在智能体模式下可配置
- 流式输出、工具执行轨迹（ThoughtChain，参数/结果可展开）、中途停止、刷新后恢复历史
- **三档确认模式**（仅 agent 模式，输入框工具栏盾牌胶囊切换，localStorage 记住选择）：
  - **按需询问（ask，默认）**：每个危险工具执行前都弹窗确认
  - **自主判断（auto）**：普通危险操作不再打扰，系统提示词注入责任准则，模型认为有风险时主动调 `request_approval(reason)` 向你申请（弹窗展示申请理由，批准才继续）；**危险的 MCP 工具在任何档位都强制弹窗**，这是硬特例
  - **完全控制（full）**：沙箱内操作一律不弹（MCP 工具仍弹）
  - 控制台 `/approve` 在 ask / full 两档间切换；命令行 `--approve` 默认进入 ask
- **内联提问卡片**：Agent 缺关键信息或需要你做抉择时会调用 `ask_user`，问题**直接渲染在助手回复气泡里**（不必自己在输入框打字）—— 给出候选时是可点选的单选 + 「其他」自定义输入，无候选时退化为纯文本输入；点击/提交后即时回传，卡片折叠为「已选择：XXX」，Agent 在**同一轮**继续执行。超时 120s 未答或中途停止按「未回答」处理（卡片转灰「已失效」），Agent 基于已有信息继续或说明所用假设。`ask / auto / full` 三档均可用；刷新页面不保留已失效卡片（答案折叠态在会话内保存）
- **附件上传**：输入区左侧加号菜单可上传图片 / 文件（多选）。图片走视觉模型（`view_image` 通道），文件内容注入消息；助手回答中引用 `.attachments/` 路径的图片会直接渲染显示
- **技能选择（输入区第三个按钮，仅智能体模式）**：树形列表按`系统内置`/`自定义`分组，点一下或回车即插入（面板随即关闭）；输入框里可放多个技能块（图标 + 名字，整块退格删除），最多 3 个、**允许重复**、前端不做去重。发送时技能名以「裸名字 + 不可见包裹字符」写进消息，后端运行时自己查找、去重并注入本轮 system prompt（顺序即优先级，正文总量上限 2 万字符、超出截断；不写进对话历史）。切到快速对话模式会移除已选技能块。输入 `/` 后继续打字会**实时模糊过滤**（忽略大小写与空格，按名称前缀 → 名称/目录名包含 → 描述包含排序），无匹配时面板自动隐藏、删字可恢复；默认高亮第一个匹配项，选中后连同 `/查询词` 一起替换成技能块
- **技能管理页**（左侧菜单「技能」，占满主区）：卡片流式布局，自定义在上、系统内置在下，宽屏一行 4 个；卡片含 64×64 主题图标（内置按名称/描述关键词匹配文档、会议、调研、日志、翻译、演示、表格、图表等主题，自定义统一用浅蓝书籍图标）、名称、描述与启用开关。点击卡片查看 `SKILL.md`（Markdown 渲染，frontmatter 不显示），自定义技能可原地编辑、`⋯` 菜单可「在对话框中试用 / 删除」；系统内置只读。停用的技能不进 system prompt 清单、不出现在输入框选择器，显式注入也忽略。删除为**软删除**（目录移到 `skills_user/.trash/`，可人工恢复）。**启停 / 编辑 / 删除仅本机可操作**：远程访问时入口隐藏，后端同样 403
- **斜杠快捷指令**：`/` 位于行首或空格之后才触发（直接接在文字后面如 `abc/` 按普通字符输入；快速对话模式不触发）。面板打开时默认高亮第一项，`↑/↓` 移动、`Enter` 选择、`Esc` 关闭；触发判定封装在 `web/src/components/composer/slashCommands.ts`，新增指令只需往注册表加一条
- **每轮消息下方工具栏**（常显）：复制按钮悬停展开「复制为 Markdown / 复制为纯文本」菜单；助手消息支持**重新生成**（回溯到对应提问重答）；用户消息支持**编辑重发**（原文回填输入框）与**删除此条及之后消息**；另有本轮 token 用量（悬停看输入/输出/模型调用/工具调用明细）与耗时。历史回放的消息没有用量与耗时，对应项自动隐藏
- **轮数用尽自动收尾**：达到「通用配置」的最大轮数上限时，自动发起一次**不带工具**的收尾调用，让模型总结「任务目标 / 做到哪一步 / 为何没做完 / 建议下一步」，并把结论**并入本轮最后一条回复** —— 整轮任务在界面上始终只呈现**一个**气泡（ReAct 中途递图片等内部注入消息不计入历史、不拆分气泡）；收尾调用失败则回退本地合成的执行事实说明，会话不会中断；该次调用的用量同样计入本轮统计
- **会话标题**：新建会话时标题先由首条消息**本地规则清洗**得到即时兜底 —— 剥掉「麻烦你帮我看一下」这类口语前缀与敬语、按停顿或词边界截断到 20 字、附件消息用文件名占位；首轮回答结束后，「记忆提炼」那次调用会**顺带**产出一个更贴切的模型标题（同一次调用，零额外 token），由后台线程写入并**锁定**（此后不再自动改写）；用户手动改过名的会话不会被覆盖，关掉记忆提炼或首轮过短时保留规则标题
- **顶栏操作**（右侧「⋯」菜单）：第一项是**智能搜索**开关（文字 + 开关，控制本会话是否联网）；下面依次是用量统计面板（消息数 / token 汇总 / 平均每轮 / 总耗时 / LLM 与工具调用次数 + 逐条明细）、导出会话为 Markdown 文件下载、清空当前会话
- **顶栏账号菜单**（最右侧用户图标）：当前账号与角色、账号管理（仅超管）、修改密码、退出登录
- **上下文水位**：输入区上方实时显示上下文占用百分比（绿 → 黄 → 红渐变预警），上限取「通用配置」的上下文字符上限
- **快捷键**：`Ctrl/⌘+N` 新建会话、`Ctrl/⌘+E` 导出 Markdown、`Ctrl/⌘+Shift+Delete` 清空会话、`Ctrl/⌘+/` 查看帮助
- **窄屏适配**：≤768px 时侧栏自动收起为浮层（汉堡按钮展开），顶栏与输入区自动紧凑布局
- **PWA（添加到主屏幕）**：内置 `manifest.webmanifest` 与图标，手机浏览器「添加到主屏幕」后
  以独立窗口运行（无地址栏）；`viewport-fit=cover` + `env(safe-area-inset-*)` 让顶栏与输入区
  避开刘海和底部横条。Service Worker 为手写极简版（只缓存静态资源、`/api/*` 一律走网络），
  但浏览器要求安全上下文 —— **局域网 http 下不会注册，属于预期**；https 或 localhost 才启用离线缓存
- **长会话分段渲染**：消息区默认只渲染最近 30 条，向上滚动到顶部自动再加载 30 条（按高度差补偿，阅读位置不跳）；内容不足以滚动时顶部显示「加载更早的 N 条消息」按钮兜底。历史气泡做了 memo（Markdown 解析 / 图表 / 工具轨迹），打字与流式输出不会重解析未变化的历史消息
- 富渲染：GFM 表格、KaTeX 公式（`$...$`）、ECharts 图表（` ```echarts ` 代码块，柱/线/饼/散点）、内联 HTML（经白名单消毒）；配套技能 `skills/rich-render/` 教模型主动用图表达
- **工作区管理**：左侧边栏按工作区分组管理对话，可新建 / 改名 / 移除；每个工作区的 `path` 就是 Agent 的文件操作边界（沙箱根），会话归属哪个工作区就在哪个目录里干活。默认工作区锚在**当前登录用户的数据目录**（如 `C:\Users\<you>\mini-agent`，启动时自动创建，不再拿源码目录当工作区），不可删除；组内还有对话时不允许删工作区
  - 输入框上方左侧有工作区胶囊：新对话可直接切换（不选就落在默认工作区），已开始的对话只读显示 —— 换沙箱根会让「之前写的文件在哪」变得难以理解
- **系统配置**：左侧抽屉底部的「系统配置」按钮打开，内含「模型配置」「通用配置」「联网搜索」「记忆」「插件管理」五个页签（顶栏点模型名也能进，直接落在模型配置页）
  - 模型配置：只维护「有哪些模型」（不设全局当前模型；具体用哪一份在对话输入框的模型菜单里按会话选择，新对话默认第一份）。任意 OpenAI 兼容接口 —— 内置 DeepSeek（默认，模型选项 `deepseek-flash` / `deepseek-v4-pro`）/ 智谱 / Kimi / 通义千问 / 豆包 / 千帆 / 硅基流动 / OpenAI / Ollama / vLLM 模板，可加多份「服务商 + 模型」配置；**显示名称默认等于模型名 ID**（输入模型名时自动带出，可手动改成更好读的名字，再次修改模型名会重新同步），对话输入框的模型胶囊以「显示名称 + 推理等级」呈现，模型子菜单按服务商分组、组内列出各配置的显示名称；带连接测试（`GET /models` 探测，不耗 token，401/403 与网络错误分开报）。配置存 `~/.mini-agent/models.json`，**API Key 只在本机，不上传第三方**，接口列表回显时脱敏为 `****后四位`
  - 通用配置：最大轮数、上下文字符上限、保留最近轮数、单次工具输出上限、温度、流式输出、并行工具、并发线程数，保存即生效（`~/.mini-agent/settings.json`，可一键恢复默认）。取值优先级：命令行显式参数 > 这里保存的值 > 环境变量 > 内置默认；页面底部还有「运行日志」开关 / 级别 / 保留天数（详见下文「运行日志」一节）
  - 通用配置 → **沙箱读取范围**（`sandbox_read`，默认 `strict`）：控制 Agent 读工作区**之外**文件的口径 —— `strict` 越界读写全拦；`whitelist` 仅白名单绝对路径可读（TextArea 每行一条）；`open` 沙箱外全部只读放开。**任何档位下，沙箱外的写入 / 删除 / 移动一律硬禁**，没有放开开关；此外 `run_python` 注入写护栏、`run_shell` 检测越界写命令、zip/tar 解压防路径穿越（混淆写法无法 100% 拦截，详见下文「沙箱」一节）
  - **联网搜索**页签：选择 `web_search` 的后端（DeepSeek 官方搜索 / Tavily / 博查 / Bing），与聊天模型解耦，任何厂商的模型都能用；**DeepSeek 联网 Key 在这里独立配置**（存 `~/.mini-agent/settings.json` 的 `deepseek_search_api_key`，`web_search` 的 Search API 与 `fetch_url` 的 Fetch API 共用），与「模型配置」里的 DeepSeek Key 完全解耦、互不引用；已配置时只显示脱敏尾号，留空保存表示不修改。开关开启时 `fetch_url` 走 DeepSeek Fetch（Markdown、按次计费），未配置 Key 或调用失败时回退本地抓取
  - **记忆**页签：编辑用户画像、开关「启用记忆 / 自动提炼」与注入预算（详见下文「记忆系统」一节）
  - **工作区记忆**（不在系统配置里）：左侧抽屉每个工作区的快捷工具区（改名按钮左边）打开，按工作区查看/编辑**项目记忆**、查看/删除该工作区的**主题摘要**
  - 插件管理：展示当前全部工具（名称 / 来源 / 分组 / 签名），按来源标记为 core / builtin / user / mcp，支持查看 dangerous / exclusive 标记与分组汇总，便于快速了解 Agent 当前具备的能力
- **右侧工作台抽屉**：顶栏最右侧图标展开/收起。初始为「开始」页，点击「工作区文件」进入目录列表；点文件新建标签页，支持代码/文本（行号+高亮）、Markdown（预览/源码）、HTML / MHTML（iframe 预览/源码，默认预览，脚本沙箱隔离）、图片、PDF（翻页/缩放）、CSV/TSV（表格分页）预览，文件行 hover 可下载；关闭全部标签后抽屉自动收起，再次打开回到开始页（只读，不修改文件）
- **运行轨迹抽屉**：助手消息脚注区 / 侧栏会话项的时钟图标打开，按 `run_id → turn` 回放会话事件流（来源着色、可展开、单轮逐条回放、按类型过滤）。与会话 JSON 双写的事实层，详见下文「可追溯运行（事件流）」一节
- **多会话与搜索**：对话本地持久化（`~/.mini-agent/sessions/*.json`，原子写），搜索支持标题 + 全文、空格分隔的多词 AND、命中片段高亮，点结果直接滚动定位到那条消息并闪烁
- **上下文记忆**：超长对话三级收敛 —— 先裁较早的工具结果、再把较早整轮滚动摘要成一段（保留任务目标/决策/进度/待办）、最后滑动窗口硬截断（保留 system + 摘要 + 最近 8 轮）。裁剪以「轮」为单位，`tool_calls` 与 `tool` 结果要么一起留要么一起删，不会配对错位导致接口报错
- **桌面化接口**：`GET /api/health` 免登录探活（返回版本 / pid / 端口 / 数据目录 / uptime）；
  `POST /api/shutdown` 优雅关闭（仅本机可用，局域网 403），关停时会通知运行中的任务收手、
  关闭 MCP 子进程；静态资源目录可用 `MINI_AGENT_WEB_DIST` 覆盖，供打包后的桌面版指向内置 dist
- 后端为 `src/core/webui.py`：stdlib `ThreadingHTTPServer` + SSE，Python 侧零新依赖；协议与设计见 [docs/WEBUI_ANTDX_PLAN.md](docs/WEBUI_ANTDX_PLAN.md)

---

## 登录与账号

默认开启登录：打开 Web UI 先看到登录页，未登录时所有 `/api/*` 一律 401。
**首次启动自动创建超级管理员 `admin` / `admin123`，登录后强制修改密码**
（未改密前除「修改密码」外的接口都返回 403）。

### 存储与安全

- 账号库 `~/.mini-agent/auth.db`（SQLite，与 `settings.json` 同目录，不进版本控制）
- 密码用标准库 `hashlib.scrypt` 加盐哈希（`n=2^14, r=8, p=1`），参数写进记录里便于日后调参；
  比较用 `hmac.compare_digest`
- 登录会话存服务端，**库里只存 token 的 sha256**，会话有效期 7 天、剩余不足 1 天自动续期；
  `last_seen_at` 按 60 秒节流写入（避免每个请求都产生一次 SQLite 写）
- Cookie `ma_session`：`HttpOnly` + `SameSite=Lax` + `Path=/`，脚本读不到、跨站表单带不上
- **Host 头白名单**：只接受回环地址、本机网卡 IP、本机主机名，挡住 DNS rebinding
  （攻击者域名解析到 127.0.0.1 时 Host 不合法，直接 403）。用反向代理或自定义域名访问时，
  通过环境变量 `MINI_AGENT_ALLOWED_HOSTS=a.example.com,b.example.com` 追加
- 连续失败 5 次锁 5 分钟（按「用户名 + IP」计）；用户名不存在时也跑一次假哈希，避免用响应时间枚举账号
- 改密后该账号的**其它**登录会话立即失效（当前会话保留）

### 账号与权限

两档角色，够用且不引入复杂度：

| 角色 | 能做什么 |
|---|---|
| `superadmin` | 使用全部功能 + 账号管理（新建、改显示名/角色、启用禁用、重置密码、强制下线、删除） |
| `user` | 使用全部功能，看不到「账号管理」入口 |

**当前不做数据隔离**：所有账号共用同一份会话、工作区、模型配置与记忆，
账号的作用是「区分谁在登录」，不是「各自独立的空间」。
账号相关的保护规则：不能删除或禁用自己；不能删除或降级最后一个启用的超管；用户名大小写不敏感唯一。

### 入口

- **登录页**：用户名 + 密码，记住用户名（不记密码）
- **顶栏账号菜单**（最右侧用户图标）：顶部显示当前账号与角色，下面是账号管理（仅超管）/ 修改密码 / 退出登录
- 会话过期或换浏览器后自动回到登录页；正在跑的 SSE 不会被打断，重新登录后用历史恢复

### 忘记密码

终端救援通道（不开放 HTTP 接口，避免变成后门）：

```powershell
python -m core --reset-password admin
```

### 关闭登录

```powershell
python -m core --web --no-login
```

仅供本机调试与自动化测试：**任何能访问该端口的人都能直接操作本机文件与命令**。
局域网部署时请不要使用。

> 已知边界：局域网走明文 HTTP，密码在链路上可被同网段嗅探 —— 只建议在可信网络中使用。

---

## 记忆系统

让智能体跨会话记住「你是谁、项目有什么规矩、之前聊过什么」。默认开启，
设置 → 记忆页签可开关与编辑用户画像；项目记忆与主题摘要按工作区隔离，入口在
左侧抽屉每个工作区的快捷工具区（改名左边）→「工作区记忆」。

### 三层

| 层 | 位置 | 作用域 | 内容 |
| --- | --- | --- | --- |
| 用户画像 | `~/.mini-agent/memory/user_profile.md` | 跨所有工作区 | 偏好、习惯、长期事实 |
| 项目记忆 | `<工作区>/.memory/project.md` | 随项目文件夹走 | 规则、已做决定、踩过的坑 |
| 主题摘要 | `<工作区>/.memory/topics/<会话id>.md` | 单个会话滚动摘要 | 最近聊了什么、进展与结论 |

项目记忆放在工作区内的 `.memory/`，是为了**随项目目录一起迁移**（拷贝、同步、
换机器都带着走）；`.memory` 创建时会加 Windows 隐藏属性，工作区是 git 仓库时
自动写入 `.gitignore`。

### 分级（信噪比）

每条记忆带一个类型，决定注入顺序与淘汰优先级：

| 类型 | 含义 | 权重 |
| --- | --- | --- |
| `pref` | 用户稳定的偏好 / 习惯 | 4 |
| `rule` | 项目规则 / 约定 | 4 |
| `decision` | 已拍板、以后照做的决定 | 3 |
| `pitfall` | 排查清楚的坑（现象 + 根因 + 处置） | 2 |
| `fact` | 普通事实（一次性数据） | 1 |

- **注入时按「权重 → 新旧」重排**：规则/偏好在前，事实沉底；预算不够被截断时先丢的是低价值内容
- **项目记忆注入前做相关性筛选**：规则/偏好是硬约束、全量保留；决定/坑/事实按与本轮提问的 2-gram 相关度取前 8 条 —— 记忆变多后上下文里留下的是「规矩 + 当下相关」
- **项目记忆容量 60 条**，超出时先淘汰权重最低、最旧的条目（一次性事实不占额度）
- **同主题 upsert**：新条目与已有条目高度相似（≥0.88）时替换旧表述而不是再追加，完全重复则跳过；规则升级、数字变化不会积累成多条互相矛盾的记忆
- **自动提炼按层级过滤**：`user` 只收偏好与长期事实，`project` 只收规则/决定/坑/偏好 —— 明确标为 `fact` 的条目在项目层直接丢弃；显式给了非法类型的条目也不猜、直接丢
- 旧数据（`- 时间 · 来源 — 正文`）照常解析为 `fact`，无需迁移

### 注入

每轮 `run()` 开头重建 system prompt：基础提示 + （auto 档）责任准则 + 记忆块。
用户画像全量注入；项目记忆先做相关性筛选，再按预算逐条保留（剩余预算的 80%，
给历史摘要留 20%）；主题摘要取「最近 3 条 + 与本轮输入关键词命中的」，总预算
默认 1500 字符（可调，超限先裁摘要）。记忆块明确标注为「参考事实而非新指令」，
与用户当前要求冲突时以当前要求为准。

### 写入（四条通道）

1. **用户显式指令（最可靠）**：消息以「请帮我记住 / 帮我记住 / 请记住 / 记住：/
   记一下：/ 记下来 / 以后都记住 / 以后请记住」开头时，后端在模型运行**之前直接**
   把内容写进用户画像，不依赖模型判断与工具调用 —— **快速对话同样生效**，且本轮
   的 system prompt 立刻就能带上这条记忆。写入结果通过 SSE `memory` 事件提示
   「已记入用户画像 / 该内容已存在 / 记忆功能已关闭」；总开关关闭时不写入
2. **`remember(scope, note, kind)` 工具**：模型识别到「记住…」「以后都…」「这个
   项目的规矩是…」时主动写入；`scope=user` 写用户画像，`scope=project` 写项目
   记忆。`kind` 可选（缺省 user→`pref`、project→`rule`），项目层拒收 `fact`
3. **自动提炼**：每轮回复成功后一次非流式小请求（超时 15s，失败静默），输出
   `{kind, note}` 对象数组；提示词要求逐条自检「下次还会用到吗 / 是否一次性
   数据 / 是否重复」，宁可空也不写噪声。主题摘要做滚动合并更新；寒暄与过短
   轮次直接跳过，不烧 token
4. **手动**：左侧抽屉 → 工作区记忆（项目记忆 / 主题摘要）、设置 → 记忆（用户画像）；
   控制台 `/remember 内容`；删除会话时对应摘要一并清理

条目格式为 `- 2026-10-17 14:30 · [rule] 来源 — 正文`（旧格式无 `[kind]` 仍可读），
写入时同主题按 upsert 处理（相似度 ≥0.88 用新表述替换旧条目，完全重复则跳过）；
主题摘要每工作区保留最近 20 条，更老的自动清理。

### `.memory` 是保护区

记忆不能由模型或脚本直接读写（避免自我篡改与误删）。以下四道防线共用同一个
判定 `core.memory.is_memory_path()`：

- 沙箱双通道：`safe_path` / `read_path` 命中即拒（即便在工作区内）
- 工具层：`list_dir` / `glob_files` / `grep` 不可见，`read_file` 读不到，
  `count_code_lines` 不计入，`zip_create` 打包时自动排除
- `run_python` prelude：`open` 读写、`pathlib` 写删、`shutil` 复制/移动/删除、
  `sqlite3` 连接全部拦截
- `run_shell`：重定向、`del`/`rmdir`/`copy`/`move`/`mkdir` 等写动词指向 `.memory`
  时拒绝

记忆模块自身读写走直连路径 + 临时文件 `os.replace` 原子写 + 进程内锁，不经过
沙箱。已知边界与沙箱一致：shell/python 的混淆写法无法 100% 拦截。

---

## 运行日志

后端日志只落本地文件，**按天一个目录**、按类别分文件，排查时不用在终端翻屏：

```text
~/.mini-agent/logs/
  2026-10-17/
    app.log     llm.log     tool.log     error.log
  2026-10-18/
    ...
```

| 文件 | 内容 |
| --- | --- |
| `logs/<日期>/app.log` | 启动与监听地址、HTTP 接口错误、会话生命周期、插件 / MCP 加载失败、记忆提炼 |
| `logs/<日期>/llm.log` | 每轮模型调用摘要（耗时、消息数、token 用量、是否流式）、流式降级与重试 |
| `logs/<日期>/tool.log` | 工具开始 / 结束、耗时、结果字符数、错误 |
| `logs/<日期>/error.log` | 所有 `ERROR` 及以上（带堆栈），任何类别都汇总到这里 |

- **不记录对话正文**：用户输入与模型输出只记字符数；工具参数只记 200 字预览。
- 开关与策略在「设置 → 通用配置 → 运行日志」：启用 / 级别（默认 `INFO`）/ 保留天数
  （默认 7 天）。跨天自动切到新日期目录，超过保留天数的旧目录自动清理。
  `DEBUG` 会额外记录每条 HTTP 请求；`INFO` 下访问日志不落盘，避免刷屏。
- 配置存 `~/.mini-agent/settings.json`（`log_enabled` / `log_level` / `log_retention_days`），
  保存即时生效，无需重启。

---

## 工作区文件抽屉

右侧抽屉用于浏览当前工作区，只读，不提供编辑保存：

- 入口：主界面顶栏最右侧的布局图标；桌面端默认 420px，可拖拽左边缘调整宽度并记住；右上角「全屏」让抽屉占满主区（保留左侧导航栏），再次点击还原。窄屏（≤1000px）为右侧浮层。
- 顶部标签页：`文件` 列表页 + 每个已打开文件一个标签；同一文件不会重复开标签。
- 文件列表：单层目录 + 面包屑，目录在前、文件在后；隐藏 `.memory` 与 `.attachments`。
- 预览格式：代码/纯文本（Prism 高亮 + 行号）、Markdown（预览/源码切换）、HTML / MHTML（sandbox iframe 预览，可切源码）、图片（png/jpg/gif/webp/bmp/svg）、PDF（pdf.js，翻页 / 缩放 / 适应宽度）、CSV / TSV（表格，行/列分页）；文件工具栏支持一键复制内容与重新加载。
- 文件列表每行 hover 出现下载按钮（触屏常显），后端以 attachment 形式返回原文件。
- 其他二进制/超大文件显示占位说明；文本最多展示 20MB（另限 5000 行、单行 4000 字符），PDF 等原始字节预览上限 20MB。
- 关闭全部标签 → 抽屉自动收起；再次打开回到「开始」页。
- 安全边界：接口严格限制在当前工作区内，`../`、绝对路径与 `.memory` 均被拒绝。

对应后端接口：`/api/workspace/dir`、`/api/workspace/file`、`/api/workspace/raw`、`/api/workspace/download`、`/api/workspace/table`、`/api/workspace/asset`。

HTML / MHTML 预览说明：

- `.html` / `.htm` 在 sandbox iframe 中渲染，默认预览，可切源码；相对 CSS / 图片 / JS 通过 `/api/workspace/asset` 加载。
- `.mht` / `.mhtml` 由后端解析（Python 标准库 `email`），提取主 HTML 与内嵌资源，资源以 `data:` URL 注入预览。
- iframe 使用 `sandbox="allow-scripts"`：脚本可运行，但没有同源权限，无法访问父页面 DOM / localStorage / cookie。

PDF 预览说明：

- 由前端 `pdfjs-dist` 在浏览器内解析渲染，原始字节走 `/api/workspace/raw`（同源 Cookie 鉴权）；超过 20MB 不预览，提示改用下载。
- 扩展新格式只需两处：后端 `_BINARY_PREVIEWS`（扩展名 → kind）与前端 `components/preview/registry.tsx`（kind → 懒加载组件）。

CSV / TSV 表格预览说明：

- 后端用标准库 `csv` 流式解析（自动处理引号、字段内换行；`.csv` 嗅探分隔符，`.tsv` 固定 Tab），按「行窗口 × 列窗口」返回，单页 200 行 × 50 列；解析上限 20MB / 10000 行 / 1000 列，解析时只保留当前窗口，内存与总行数无关。
- 前端表格冻结表头与序号列，行/列各自翻页；首行作为表头，空列名补 `列N`，重名列加后缀。

---

## 可追溯运行（事件流）

每一次 `run()` 的「事实」都会按发生顺序追加到 `~/.mini-agent/sessions/<会话id>.events.jsonl`
（append-only，一行一个 JSON 信封），Web UI 的「运行轨迹」抽屉直接回放这份事实。

- **入口**：助手消息脚注区的时钟图标（hover 提示「查看运行轨迹」）；侧栏会话项 hover 的同一图标（会先切到该会话）。抽屉与右侧工作台共用一套外壳/宽度记忆，二者互斥。
- **信封**：`{v, seq, ts, type, source, phase, turn, run_id, payload}`；`type` 共 12 种 —— `run_start` / `system_prompt` / `user_message` / `context_inject` / `llm_request` / `llm_response` / `tool_call` / `tool_result` / `approval` / `cancel` / `error` / `run_end`。`source` 为 `system | user | memory | model | tool | approval | loop`，决定轨迹行的着色。
- **视图**：按 `run_id → turn` 两级分组的时间线；每行显示序号 / 时间 / `type` 徽标 / 一行摘要，hover 出现「详情」按钮 → 展开为语法高亮的原始内容（换行不转义）。顶部过滤器 `全部 / 模型 / 工具 / 上下文 / 授权` + 「只看本轮」；每个轮次分组头有「回放」按钮，按时间顺序逐条揭示该轮事件（纯前端展示，不重发请求）。
- **关键不变式（I1）**：`llm_request` 只存逐条消息的 `sha256` 摘要（不存正文），正文各自只存一次在 `system_prompt` / `user_message` / `llm_response` / `tool_result` 等来源事件里 —— 事件流体积不膨胀，且仅凭事件流即可完整、可校验地重建每次请求的消息列表（图片以 `#img:<sha256>` 占位）。
- **与会话 JSON 的关系**：`.json` 仍是恢复历史、搜索、展示的主口径，事件流是事实层与兜底（双写）。删除会话时两个文件一起删。
- **零侵入**：Agent 未挂 `event_writer`（CLI / REPL / 未启用）时埋点是纯 no-op，行为与耗时和接入前逐条一致；写入失败只吞掉，绝不影响任务。
- **实时镜像**：运行中 SSE 会附带 `trace` 帧把事件即时推进抽屉，但**不阻塞**；收到 `done / error / cancelled` 后前端再拉一次全量做磁盘兜底。
- **开关**：设置 → 通用配置 → 「运行轨迹 → 记录会话事件流」（`events_enabled`，默认开）。关闭后不再产生新文件；旧会话（早于本能力）打开轨迹显示「无事件记录」而不报错。

对应后端接口：`GET /api/trajectory?session=<sid>&from_seq=<int>&limit=<int>`（只读；默认 500、上限 5000；无事件文件返回空数组）。

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

Web UI 里还可以**显式指定**：输入区第三个按钮（或输入 `/`）插入技能后，发送时技能名
以「裸名字 + 不可见包裹字符」写进消息；后端运行时从消息里查找（重复只算一次、
最多取 3 次出现），把对应正文直接附加到**这一轮的 system prompt**（`Agent.run` 的
`extra_system`），不依赖模型自己判断，也不会写进对话历史。技能来源分两类：
`skills/` → 系统内置、`skills_user/` → 用户自定义（未来由对话自动生成的技能也归入
这一类）。快速对话模式不解析、也不提供技能选择。

这样技能写再长，也只在不相关的任务里占一行清单的位置，
几十个技能也不会吃掉上下文 —— 这是「清单 + 按需加载」相比「全部塞进
system prompt」的决定性差异。

**自带技能（`skills/`）：**

| 名称 | 用途 | 默认产出 |
| --- | --- | --- |
| `CSV 分析` | CSV 数据分析，结论可复核 | 对话内 |
| `UI 渲染` | 富渲染输出约定（ECharts / 公式 / 表格） | 对话内 |
| `文档写作` | 需求说明、方案、手册等成篇文档 | `docs/` |
| `会议纪要` | 记录整理成结论、待办、责任人 | `docs/` |
| `调研简报` | 检索 + 交叉验证，一页式简报 | `reports/` |
| `日志排错` | 从日志定位根因，给修复与验证 | `reports/`（按需） |
| `翻译润色` | 中英互译与润色，术语统一 | 对话内 |
| `演示大纲` | 逐页 PPT 大纲 + 讲稿要点 | `docs/` |

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
| `/remember [user\|project] 内容` | 手动写入一条长期记忆（默认写当前项目记忆） |
| `/memory`           | 查看当前生效的用户画像 / 项目记忆 / 主题摘要 |
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

**十九个免费用例套件，全部不需要 API Key、不花任何费用**（另有 1 个真实用例默认跳过，加 `--real` 才跑）。  
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
| `tests/test_skills.py` | 技能层 10 项：frontmatter / 加载 / 冲突 / 热重载 / use_skill / 截断 / 来源分类 / 启停状态 / 消息解析 / 本轮注入 |
| `tests/test_vision.py` | 图片输入 5 项：嗅探 / 附件通道 / 拒绝 / 循环集成 |
| `tests/test_stream_parallel.py` | 流式聚合 + 并行执行 + MCP 并发 19 项 |
| `tests/test_cancel.py` | 取消机制 10 项：令牌 / 杀进程 / 流中断 / 消息补齐 / 原因送达 |
| `tests/test_mcp_cancel.py` | MCP 取消通知 8 项：送达 / 停手 / 迟到响应 / 幂等 / 定向 |
| `tests/test_http_e2e.py` | 本地 mock HTTP（支持 SSE）：流式三条降级路径 + 并行收益 |
| `tests/test_webui.py` | Web UI 端到端 17 项：SSE 事件序列 / 双模式工具表 / 停止 / 危险确认 / 409 / 工作区 / 模型配置 / 会话搜索 / 通用配置 / 用户指令记忆 / keep-alive 请求体 |
| `tests/test_sys_monitor.py` | 系统监控插件 8 项：CPU / 内存 / 磁盘 / 网络 / 开机时长 / 端口占用 |
| `tests/test_memory.py` | 记忆系统 15 项：存储 / 保护区 / 工具不可见 / prelude / shell / 注入 / 分级淘汰 / 提炼过滤 / 检索合并 / 显式指令 |
| `tests/test_logging.py` | 文件日志 6 项：按日目录 / 错误聚合 / 幂等重配 / 关闭 / 配置 / 清理 |
| `tests/test_workbench.py` | 工作台文件接口 12 项：目录 / 安全 / 编码 / 图片 / HTML / MHTML / PDF / 下载 / 表格 |
| `tests/test_skill_admin.py` | 技能管理接口 5 项：列表字段 / 详情 / 启停 / 编辑（内置 403、重名拦截）/ 软删除 |
| `tests/test_events.py` | 事件流 8 项：只追加 / 请求可重建 / 裁剪留痕 / 开关 / 兜底 |
| `tests/test_model_config.py` | 推理等级通用化 8 项：厂家 spec（low/high/max）/ 三种参数风格与 values 重映射 / 迁移 / 会话解析 |
| `tests/test_web_search.py` | 联网搜索 3 项：后端选择 / DeepSeek Key 复用 / 请求与解析 |
| `tests/test_net_tools.py` | 网络工具 4 项：云端/本地分派 / 失败回退 / 协议校验 / Fetch 请求解析 |
| `tests/test_ask.py` | 内联提问 12 项：返回协议 / 选项规范化 / 工具契约 / 等待桥 |
| `tests/test_auth.py` | 登录与账号 9 项：建库 / 默认超管 / scrypt 哈希 / 会话 / 失败限流 / 超管保护规则 / 改密 / 写入节流 |

**CI**：仓库带 `.github/workflows/ci.yml`，push/PR 时自动跑两件事 ——
Windows runner 上 `pytest tests -q`（平台行为依赖 Windows），Ubuntu 上 `npm run build`
（含 `tsc --noEmit`）。测试通过 `tests/bootstrap.py` 把数据目录指向临时目录，
不会读写真实的 `~/.mini-agent`。

实测（`.venv` 内，Windows）：

```
  ✓ test_loop.py                 1.54s
  ✓ test_plugins.py              2.16s
  ✓ test_skills.py               0.22s
  ✓ test_vision.py               1.40s
  ✓ test_stream_parallel.py      4.53s
  ✓ test_cancel.py               6.63s
  ✓ test_mcp_cancel.py           6.47s
  ✓ test_http_e2e.py             7.14s
  ✓ test_webui.py                5.85s
  ✓ test_sys_monitor.py         43.12s
  ✓ test_memory.py               2.39s
  ✓ test_logging.py              0.26s
  ✓ test_workbench.py            1.61s

  13/13 通过 | 累计耗时 83.3s
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
│   ├── memory.py             记忆系统：三层存储 / 保护区判定 / 注入渲染 / remember 工具
│   ├── events.py             会话事件流（可追溯运行）：append-only JSONL、EventWriter、按事件重放消息
│   ├── webui.py              Web UI 服务：stdlib ThreadingHTTPServer + SSE、工作台/会话/设置接口
│   ├── workspace_config.py   工作区配置：注册表与路径解析（resolve_path）
│   ├── model_config.py       模型配置：多服务商配置读写与连接测试（~/.mini-agent/models.json）
│   ├── settings.py           通用配置：读写 settings.json 并应用到运行时
│   ├── logging_setup.py      文件日志：按日目录分文件（app/llm/tool/error）、保留清理、幂等重配
│   ├── mhtml.py              MHTML（.mht/.mhtml）解析：拆出主 HTML 与内嵌资源（data: URL）
│   ├── python_prelude.py     run_python 写护栏（open/shutil/pathlib/sqlite 等 patch）
│   ├── cancel.py             取消令牌：CancellationToken / Cancelled / 杀进程树
│   ├── mcp.py                MCP stdio 客户端（并发安全 + 可取消 + 取消通知）+ 桥接
│   └── images.py             图片附件通道（view_image 的底层）
├── plugins/                  自带插件（24 个，按分组）
│   ├── fs_tools.py           文件系统：read_file / write_file / list_dir / glob_files / grep
│   ├── file_ops.py           文件操作
│   ├── code_tools.py         代码辅助：edit_file（唯一匹配块编辑）/ re_replace /
│   │                          search_symbol / py_syntax_check / count_code_lines
│   ├── exec_tools.py         命令执行：run_shell / run_python
│   ├── net_tools.py          网络：fetch_url
│   ├── net_extra_tools.py    网络增强
│   ├── web_search.py         联网搜索：web_search（DeepSeek 官方 Search / Tavily / 博查 / Bing，可在系统配置切换）
│   ├── vision_tools.py       视觉：view_image（图片输入：按魔数嗅探格式，附件进对话）
│   ├── image_meta_tools.py   图片元信息
│   ├── data_tools.py         结构化数据：read_csv / write_csv / read_json / read_jsonl / file_info / detect_encoding
│   ├── dataframe_tools.py    大表格分析（polars）：df_summary / df_sql / df_to_parquet
│   ├── office_tools.py       办公文档：read_docx / write_docx / docx_to_markdown / read_xlsx / write_xlsx /
│   │                         read_pdf / read_pdf_tables / write_pdf
│   │                         （docx_to_markdown 用 mammoth→HTML→markdownify 高保真转换，保留表格；
│   │                          read_xlsx 优先用 python-calamine 引擎，可读 .xls/.xlsb/.ods 且更快；
│   │                          write_pdf 内置中文字体）
│   ├── ocr_tools.py          扫描件 OCR（pypdfium2 + rapidocr-onnxruntime）：ocr_image / ocr_pdf
│   │                         （首次调用需初始化引擎；read_pdf 提不出文字时用它）
│   ├── database_tools.py     数据库
│   ├── archive_tools.py      压缩归档
│   ├── git_tools.py          Git
│   ├── hash_tools.py         哈希与编码
│   ├── random_tools.py       随机与标识
│   ├── text_extra_tools.py   文本增强
│   ├── time_tools.py         时间
│   ├── time_math_tools.py    时间运算
│   ├── sys_tools.py          系统信息
│   ├── sys_extra_tools.py    系统增强
│   └── sys_monitor.py        系统监控：CPU / 内存 / 磁盘 / 网络 / 开机时长 / 端口占用
├── plugins_user/             你的插件（含示例 text_tools.py 与开发指南）
├── skills/                   自带技能（8 个，清单见「加一个技能」）
├── skills_user/              你的技能（含约定说明，一个目录 + SKILL.md）
├── mcp/
│   ├── mcp_servers.json      MCP 配置
│   └── mcp_demo_server.py    自带 MCP server（now / sha256 / env_info / sleep /
│                             stubborn_sleep / cancel_log，后两个服务于取消通知测试）
├── tests/                    二十个套件（19 免费 + 1 真实）+ bootstrap.py（共用引导）+ run_tests.py
├── scripts/                  setup_venv.* / run_tests.* / make_demo_case.py
├── docs/                     测试指南与历次专题报告
├── demo_case/                沙箱测试数据（有标准答案）
└── pyproject.toml            src 布局 + console script
```

> 可写数据（配置 / 会话 / 账号库 / 日志）不在仓库里，统一放在 `~/.mini-agent/`，
> 详见「数据目录」一节。

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

当前是**可扩展的地基**，不是生产系统。要上线还缺：

- **权限规则引擎**：判断粒度只有「整个工具是否 dangerous」这一层，读范围也只有一套
  对所有工具生效的全局策略。缺按工具 / 参数 / 路径逐条配置的规则
  （如「`read_file` 只允许某目录」「`run_shell` 禁止某类命令」），
  无法为不同场景配不同策略。
- **跨运行追踪与成本归因**：`run_id` 只标识单次运行，不串成 trace，
  回答不了「一次用户请求跨了几次运行、触发了哪些工具」。
  也缺单价表与金额换算，无法按模型 / 会话 / 工作区统计费用。
- **语义检索**：记忆召回依赖字符重合与关键词计数，同义改写（「部署」/「上线」）
  命中不了。缺向量索引与 embedding，条目变多后召回会持续退化。
- **MCP 双向能力**：缺 resources / prompts / sampling 的调用入口；
  服务端主动发来的 `notifications/progress` 与 `roots/list` 无人处理
  （入站带 `id` 的请求落进 `_rx` 后没有消费者），进度与反向请求都会丢失。
- **后端语音能力**：只有浏览器 Web Speech API，依赖厂商云端、Firefox 不可用；
  缺离线 ASR（本地模型）与 TTS 输出，无法满足「数据不出本机」。
- **线程级取消**：纯 Python 阻塞调用无法被取消令牌打断（Python 不能安全杀线程），
  长耗时工具只能等它自己返回；MCP 侧也仍缺服务端请求的应答通道。
