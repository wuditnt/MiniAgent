# mini-agent Web UI 方案（Ant Design X · v2 双模式）

> 历史文档：术语按当时口径（插件 / 工具混用），正文未改写。

> 状态：已实施（2026-10-15） ｜ 日期：2026-10-15 ｜ v2：新增 chat/agent 双模式与联网搜索开关
> 结论先行：后端坚持零 Python 依赖（stdlib `ThreadingHTTPServer` + SSE），
> 前端用 React + Vite + Ant Design X；**双模式 = 每会话定制工具表**，agent 核心不动；
> 联网搜索 = 新增 `web_search` 插件，用户在界面上开关。

---

## 1. 背景与目标

mini-agent 目前只有终端控制台（`repl.py` + `RichUI`）。`core/ui.py` 已经抽象出 9 个 UI 回调，
注释中明确预留了「Web UI 只要再实现一个 UI 类」的扩展点。

本方案目标：

- 浏览器聊天界面，本地化部署（默认只监听 `127.0.0.1`）
- **两种交互模式**：chat（纯对话）/ agent（工具循环），界面上随时切换
- **联网搜索**作为用户可选开关，两种模式下行为不同（见 §3）
- 完整保留 agent 现有能力：流式输出、工具执行轨迹、危险操作确认、任务取消、并行工具
- Python 侧零新依赖；Node 仅在构建前端时需要

## 2. 技术选型

| 层 | 选型 | 版本 | 说明 |
| --- | --- | --- | --- |
| 前端框架 | React | 18.x | Ant Design X 要求 ≥18 |
| 构建工具 | Vite | 6.x | dev 代理 + 生产构建 |
| 组件库 | antd | 5.x | 基础组件（含 Segmented / Switch） |
| AI 对话组件 | **@ant-design/x** | 1.x（锁定小版本） | Bubble / Sender / ThoughtChain / XStream |
| Markdown 渲染 | react-markdown | 9.x | 气泡内容渲染 |
| 后端 | stdlib `http.server` | Python 3.10+ | 已在 test_http_e2e.py 验证过 SSE 链路 |
| 通信 | SSE（POST + fetch 流） | — | `EventSource` 只支持 GET，改用 fetch + `XStream` 解析 |

本机环境已确认：Node v24.12.0 / npm 11.6.2。

## 3. 交互模式与联网搜索（v2 核心）

### 3.1 行为矩阵

模式与联网开关的组合，落到**每会话工具表**上（`Agent` 持有独立 `self.registry`，
[agent.py L320](../src/core/agent.py) 的 `run()` 用它生成 schemas 并执行工具）：

| 模式 | 联网搜索 | 会话工具表 | 行为 |
| --- | --- | --- | --- |
| chat | 关 | `{}` | 单次 LLM 直答，无工具事件，最快 |
| chat | 开 | `{web_search}` | 模型自主「先搜后答」；搜索过程以 tool 事件显示 |
| agent | 关 | 全量 68 个内置工具 | 现有完整工具循环 |
| agent | 开 | 全量 + `web_search` | 工具循环 + 模型按需搜索 |

实现机制：webui 为每个会话用 `use_registry(定制表)` 上下文构造该会话的 `Agent`，
定制表按矩阵组合（克隆全局 REGISTRY 后增删）。
实现时需验证工具执行路径走的是 `self.registry.call`（预期如此）；若走全局表，改 agent.py 约 1-2 行。

### 3.2 请求参数与切换语义

- `POST /api/chat` body 增加 `mode: "chat" | "agent"`、`web: bool`
- 模式/开关**逐消息生效**：切换后下一条消息按新配置选择工具表；历史对话保留
- UI 上模式记忆在 localStorage；`GET /api/state` 返回当前默认值

### 3.3 web_search 插件（plugins/web_search.py）

后端可插拔的检索工具，按优先级自动选择：

| 优先级 | 后端 | 条件 | 说明 |
| --- | --- | --- | --- |
| 1 | Tavily REST | 环境变量 `TAVILY_API_KEY` | 质量最好，专为 LLM 设计 |
| 2 | 博查 Bocha | 环境变量 `BOCHA_API_KEY` | 国内合规、中文质量好 |
| 3 | Bing CN 抓取 | 无需 key | `cn.bing.com/search?q=` 解析标题/摘要/链接，兜底国内可达 |

- 工具签名：`web_search(query, max_results=5)`，返回「标题 / URL / 摘要」列表（每条摘要截断）
- 只读、非 dangerous；走 urllib，不引入新依赖
- 独立于 Web UI 可先行交付：`python -m core --tools` 可见，终端对话即可测试
- 风险：Bing 反爬可能失败 → 失败时返回 `[error] 搜索失败，建议配置 TAVILY_API_KEY`，不阻塞对话

## 4. 架构总览

```
┌──────────────┐   HTTP + SSE    ┌─────────────────────────────────────┐
│   浏览器      │ ◄─────────────► │  mini-agent 本地进程（Python）        │
│  React + X   │  POST /api/chat │  ┌─────────┐   UI 回调   ┌────────┐ │      ┌───────────┐
│  Bubble.List │  POST /api/stop │  │ WebUI   │ ──────────► │ Agent  │ │ ───► │ LLM API   │
│  Sender      │  POST /api/confirm  │ (新)    │   9 个回调   │ 循环    │ │      │ OpenAI 兼容│
│  ThoughtChain│                 │  └────┬────┘             └────┬───┘ │      └───────────┘
│  Segmented ▣ │                 │       │ Queue(SSE 事件)       ▼     │
└──────────────┘                 │  core/webui.py   每会话工具表+web_search│
                                 └─────────────────────────────────────┘
```

关键点：

- `WebUI` 实现 [ui.py](../src/core/ui.py) 的 9 个回调，每个回调把事件推入 `queue.Queue`
- `POST /api/chat` 的工作线程跑 `agent.run()`；请求线程从 Queue 逐条取事件写成 SSE 帧返回
- agent 核心代码一行不改（工具表差异化即可实现双模式）

## 5. HTTP API 设计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat` | body: `{"message", "mode": "chat"\|"agent", "web": bool}`；响应为 SSE 事件流 |
| GET | `/api/history` | 返回当前会话消息历史（页面刷新后恢复） |
| GET | `/api/state` | 元信息：工作区、模型名、工具数、agent 状态、搜索后端检测结果 |
| POST | `/api/confirm` | body: `{"id", "approve"}`；放行/拒绝危险操作 |
| POST | `/api/stop` | 触发取消令牌（复用 `core/cancel.py`） |
| GET | `/` 及静态资源 | 生产模式托管 `web/dist`；dev 模式由 Vite 提供 |

## 6. SSE 事件协议

每帧格式：`data: {JSON}\n\n`。前端用 `XStream` 解析。**双模式不新增事件类型**：
chat+联网下的搜索调用复用 `tool_start/tool_end`（name=web_search），前端 ThoughtChain 原样渲染。

| type | 载荷 | 对应 UI 回调 | 前端处理 |
| --- | --- | --- | --- |
| `turn` | `{turn, mode}` | `turn_start` | 更新轮次与模式角标 |
| `delta` | `{text}` | `llm_delta` | Bubble 流式追加 |
| `llm_done` | `{sec, streamed}` | `llm_done` | 结束流式态 |
| `tool_start` | `{id, name, args}` | `tool_start` | ThoughtChain 新增 pending 节点 |
| `tool_end` | `{id, ok, sec, result}` | `tool_end` | 节点完成/失败 + 耗时；结果可展开 |
| `confirm_request` | `{id, tool, args, danger}` | `ask_confirm` | 弹 Modal；等待用户选择 |
| `notice` | `{text}` | `notice` | 系统提示条 |
| `cancelled` | `{reason}` | `cancelled` | 终止态气泡 |
| `done` | `{answer, sec}` | —（合成） | 关闭流、解锁输入框 |
| `error` | `{message}` | —（合成） | 错误气泡 |

危险确认的阻塞桥接：`ask_confirm` 回调推 `confirm_request` 事件后，
在 `threading.Event().wait()` 上挂起；浏览器 `POST /api/confirm` 后 set 事件，返回用户选择。
不设超时：一直等到用户作答；点停止或浏览器断连（SSE 断开）才按拒绝/未回答处理，防止会话卡死。

## 7. 前端组件映射（Ant Design X）

| X 组件 | 用途 |
| --- | --- |
| `Bubble.List` | 消息流；assistant 气泡自定义渲染（Markdown + 工具轨迹） |
| `Sender` | 输入框；`loading` 态自动变「停止」按钮 → `POST /api/stop` |
| `ThoughtChain` | 工具执行轨迹：节点 = 工具名，状态 pending/success/error，可展开看参数与结果 |
| `Welcome` | 欢迎页，**按模式区分文案**（chat：写作/翻译/问答；agent：任务执行） |
| `Prompts` | 快捷指令**双套**：chat 套（总结/润色/翻译）与 agent 套（统计代码行数/系统信息/分析 CSV/git 状态） |
| `Segmented`（antd） | 头部模式切换：对话 / 智能体 |
| `Switch`（antd） | 头部联网搜索开关（关闭态灰显；`/api/state` 报告可用后端） |
| `Conversations` | 左侧会话列表（二期：多会话） |
| `Attachments` | 文件上传（二期：配合 read_csv / file_hash 等工具） |
| `useXAgent` + `useXChat` | 消息状态机；`request` 自定义为 fetch + XStream 消费 SSE |

页面布局：顶部状态栏（模型名/工作区/模式 Segmented/联网 Switch/连接状态），
中部 `Bubble.List`，底部 `Sender`。

## 8. 目录结构

```
web/                        # 前端工程（仅构建期需要 Node）
├── package.json            # 锁定 @ant-design/x 小版本
├── vite.config.ts          # dev 代理 /api → http://127.0.0.1:8000
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx             # 布局 + 模式/开关状态编排
    ├── api.ts              # SSE 消费：fetch POST → XStream → 事件回调
    ├── types.ts            # SSE 事件与消息类型定义
    └── components/
        ├── Chat.tsx        # Bubble.List + Sender
        ├── ToolChain.tsx   # ThoughtChain 封装（tool_start/end → items）
        ├── ConfirmModal.tsx# 危险操作确认
        ├── ModeBar.tsx     # Segmented + Switch 头部
        └── WelcomePane.tsx # Welcome + Prompts（双套快捷指令）

plugins/
└── web_search.py           # 新增：联网搜索插件（Tavily/Bocha/Bing CN 兜底）

core/
└── webui.py                # 新增：HTTP 服务 + WebUI 类 + 每会话工具表

tests/
└── test_webui.py           # 新增：起真实服务，模拟对话，断言 SSE 事件序列
```

## 9. 后端实现要点（core/webui.py）

1. **`WebUI(BaseUI)`**：实现 9 个回调 → 全部翻译成事件推入 `Queue`；带会话注册表
   `{session_id: SessionState(agent, queue, confirm_event, token, mode, web)}`
2. **每会话工具表**：按 §3.1 矩阵用 `use_registry()` 构造会话 Agent；
   chat 模式下也可换用更轻的 system prompt（实现时定，非必须）
3. **chat 流程**：请求线程创建队列 → 起工作线程跑 `agent.run(message)` →
   循环 `queue.get(timeout)` 写 SSE 帧，直到 `done/error/cancelled` 帧
4. **静态托管**：`web/dist` 存在则托管；不存在返回提示「请先 npm run build」
5. **安全**：默认 `--host 127.0.0.1`；`safe_path` 沙箱不变；无 Cookie/CSRF 面（本机单用户）
6. **CLI**：`main.py` 增加 `--web`、`--port`（默认 8000）、`--host`

## 10. 构建与部署

```powershell
# 开发模式（两个终端）
python -m core --web            # 终端 1：后端 :8000
cd web; npm install; npm run dev  # 终端 2：Vite :5173，API 自动代理

# 生产模式（运行时无需 Node）
cd web; npm run build           # 产出 web/dist
python -m core --web            # Python 托管 dist，浏览器开 http://127.0.0.1:8000
```

`web/dist` 加入 `.gitignore`；README 记录构建步骤。可选 `scripts/build_web.ps1` 一键构建。

## 11. 实施里程碑

| 阶段 | 内容 | 验证方式 |
| --- | --- | --- |
| M0 | `plugins/web_search.py`（独立于 Web UI 先行） | `--tools` 可见；终端对话问时效问题触发搜索 |
| M1 | `core/webui.py`：SSE 骨架 + chat/confirm/stop + 每会话工具表 | curl 手测事件流；chat/agent 两模式 curl 验证 |
| M2 | Vite + X 骨架：Bubble 流式 + Sender + Segmented/Switch | 浏览器看到逐字输出；切模式生效 |
| M3 | ThoughtChain 工具轨迹 + ConfirmModal + 停止按钮 | 触发 delete_file 看确认弹窗；中途停止 |
| M4 | Welcome/Prompts 双套/Markdown/状态栏打磨 + 生产构建 | dist 模式全功能回归 |
| M5 | `tests/test_webui.py`：事件序列断言（含双模式） + 全量测试 | 10/10 通过 |

## 12. 风险与对策

| 风险 | 对策 |
| --- | --- |
| @ant-design/x 迭代快、API 变动 | package.json 锁定精确版本；封装 `api.ts` 隔离 |
| npm 国内安装慢 | 配置 npmmirror registry |
| Bing 反爬导致兜底搜索失败 | 返回友好 error 并提示配 Tavily/Bocha key；不阻塞对话 |
| chat 模式模型不调 web_search（该搜没搜） | 工具描述强化时效性提示；必要时 chat+web 下系统提示加一句引导 |
| 工具执行未走 self.registry（会话隔离失效） | M1 实现时验证；若走全局表，agent.py 加 1-2 行参数透传 |
| SSE 响应被缓冲不逐帧到达 | 每帧后 `wfile.flush()`；响应头 `Cache-Control: no-cache` |
| 确认 Modal 无人响应导致会话卡死 | `confirm_event.wait(120)` 超时自动拒绝 |
| XStream 兼容性 | 降级路径：fetch 流手写 SSE 解析（~30 行），api.ts 内置 fallback |
| dist 未构建导致白屏 | 后端检测 dist 缺失时返回友好引导页 |

## 13. 验收标准

- [ ] chat 模式（联网关）：单次直答，全程无工具事件
- [ ] chat 模式（联网开）：问「今天/最新」类问题时先搜后答，ThoughtChain 显示 web_search 节点
- [ ] agent 模式（联网开）：68+1 个工具可用；工具轨迹、危险确认、停止全部正常
- [ ] 模式切换逐消息生效，历史保留；开关状态刷新后记忆
- [ ] 刷新页面后历史消息恢复（`/api/history`）
- [ ] 生产 dist 模式全功能可用，无需 Node
- [ ] `python -m core --tools` 可见 web_search；`tests/run_tests.py` 10/10 通过
