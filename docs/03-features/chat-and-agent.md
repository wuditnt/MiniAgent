# 对话与智能体

## 用途

Web UI 的全部交互都走在这套「双模式」上：同一个会话里逐条消息决定本轮是快速对话还是完整智能体循环。

## 概念

| 模式 | 做了什么 | 工具范围 |
|---|---|---|
| **快速对话（chat）** | 只做问答、解释、写作、推理；**显式关闭思考**，不加载全量工具 | 少量即时工具（时间、本机概况、联网搜索/抓取），无文件读写与命令执行 |
| **智能体（agent）** | 完整 ReAct 循环：模型可以反复调工具直到完成 | 全量内置工具 + 用户工具 + MCP 工具 |

模式**按消息**切换、历史保留：会话里前一条是 agent、后一条可以是 chat。会话 JSON 记录每条消息所用的模式。

## 使用方式

- 侧栏新建会话时有「对话 / 任务」两个按钮，决定初始模式；之后在输入框区域切换。
- 切到快速对话会移除已选的技能块（技能只在智能体模式可用）。
- 快速对话不显示推理等级选择；智能体模式可选 Low / High / Max。
- 「联网搜索」开关在输入框上方右侧，按会话独立保存，两种模式都可用。

## 输入框与上下文注入

输入框支持三种「随消息带上下文」的方式，内容不会进入正文气泡：

| 方式 | 入口 | 服务端处理 |
|---|---|---|
| **@ 引用工作区文件** | 行首或任意空白后键入 `@`，按文件名 / 相对路径搜索 | 发送时按相对路径读取快照：文本 ≤ 8KB 内联全文，> 8KB 给前 40 行摘要，图片 ≤ 24MB 走多模态，其余只给路径。单条最多 5 个，内联总预算 32KB，超出按顺序降级为路径引用 |
| **附件上传** | 回形针 / 图片按钮、拖拽到输入区、粘贴截图 | 复制进工作区 `.attachments/`；单文件 ≤ 24MB，单条最多 5 个；图片走多模态，文本注入 8000 字节选，其它二进制只记路径 |
| **技能块** | `/` 唤起技能面板（仅智能体模式） | 技能名随消息发送，运行时把技能正文临时附加到 system prompt；不写入历史 |

交互约定：

- `@` 与 `/` 两个面板互斥；`Esc` 关闭，`↑↓` 移动，`Enter` / `Tab` 选中；选中文件后以绿色 chip 留在输入框，可整块退格删除。
- `@` 面板为空查询时展示「最近使用」（按工作区存 localStorage）+「从本地上传…」，输入查询词后才请求服务端搜索（150ms 防抖）。
- 工作台文件树的文件也可直接拖进对话区：松手即变成引用 chip（自定义 MIME `application/x-miniagent-ref`，见 `refDrag.ts`）；系统文件拖入则走附件上传。整块对话区都是热区，拖入时显示提示遮罩。
- 工作区引用是**发送时刻的快照**：之后文件被改 / 被删，历史气泡只标注「已变更 / 已删除」，不重读内容。
- 远程访问为只读：禁止上传附件与新增引用，历史引用仍可查看。
- 自动化任务不携带 `@` 引用（P0）。

## 图形与代码块

助手回答里的三种围栏会被渲染成图形卡片，而不是普通代码块：

| 围栏 | 用途 |
|---|---|
| `` ```echarts `` | 数据图表，内容是 ECharts option JSON |
| `` ```mermaid `` | 流程图 / 时序图 / 状态图 / 类图 / ER 图 / 甘特图 |
| `` ```svg `` | 自绘矢量图，内容是完整 `<svg>` 元素 |

卡片能力一致：视图 / 代码切换（代码视图带行号）、复制、下载（SVG / PNG）、全屏预览（只撑满对话区，不是整个浏览器）。会话内卡片是静态展示：不拦截滚轮、不支持拖动与缩放，避免鼠标移到图上时页面滚不动；点开全屏后才启用拖动平移、滚轮缩放与复位 / 放大 / 缩小。

流式生成时先出现卡片外壳 + 白色蒙版「绘图生成中…」，内容收齐后一次性展示，不会先闪出半截源码。PNG 导出会剔除 `<foreignObject>`（用 `htmlLabels:false` 重渲染），否则 canvas 会被判定污染而失败。

## 核心循环

```
run(user_input)
  ├─ 重建 system prompt（基础提示 + 记忆 + 技能清单 + auto 档责任准则）
  ├─ _trim_context()      超长对话三级收敛
  ├─ 请求模型（流式优先，失败逐级降级）
  ├─ 模型返回 tool_calls？
  │    ├─ 是 → _run_tools()：主线程确认 → 线程池并发 → 主线程按序回填 → 下一轮
  │    └─ 否 → 结束，返回最终回答
  └─ 达到 max_turns → 一次不带工具的收尾调用
```

流式聚合要点：

- `delta.content` 直接累加；
- `tool_calls` 按 `index` 聚合：`id` / `name` 只出现一次，`arguments` 逐片累加；
- `usage` 在 choices 为空的统计帧里；
- 三级降级：不支持 `stream_options` → 去掉重试；不支持 `stream` → 普通请求；答应了流式但一帧不发 → 本轮改普通模式重试。

## 取消

- 每次 `run()` 新建 `CancellationToken`，设为「当前令牌」；工具 / 流式分片 / MCP 等待循环 / 轮次之间都轮询它。
- 触发时杀整棵进程树（Windows `taskkill /T`）、关闭 HTTP 客户端、向 MCP 对端发 `notifications/cancelled`、调 `ui.cancelled(reason)`。
- 取消后 `_patch_cancelled_replies()` 把缺失的 tool 回复补成 `[cancelled] ...`，保证消息序列仍然合法。
- `KeyboardInterrupt` 归一化为 `Cancelled`，调用方只接一种异常。

## 收尾与上下文

- **轮数用尽收尾**：达到最大轮数时发一次不带工具的调用，输出「任务目标 / 做到哪一步 / 为何没做完 / 建议下一步」，并入本轮最后一条回复；失败则本地合成事实说明。
- **收尾验证门**：本轮产出或修改了交付物（写文件、改外部状态等）时，注入一条系统提醒做内部自查；只读命令（取色、统计、语法检查）与纯读任务不触发。自查过程不写进最终答复。
- **成稿改写**：最终答复命中过程泄漏特征（验证依据、复核、修正记录、MD5 等）且用户没要求核对细节时，额外调一次非流式模型做删减重组；只删过程、保留结论与数字，失败/取消自动回退草稿。仅智能体模式且有工具调用时生效，快速对话不受影响；开关 `rewrite_final_answer`。
- **上下文水位**：输入区上方显示占用百分比；超长时先裁旧工具结果，再把较早整轮摘要，最后滑动窗口硬截断（保留 system + 摘要 + 最近 8 轮）。
- **任务执行归后端**：跑在服务端 worker 线程，关页面只停推送不中断执行；同一会话后台运行时发新消息返回 409。

## 关键代码位置

- 循环与取消：`src/core/agent.py` 的 `Agent.run()` / `_run_loop()` / `_request()` / `_consume_stream()` / `_run_tools()` / `interrupt()` / `_patch_cancelled_replies()`
- 上下文：`_trim_context()` / `_trim_old_tool_results()` / `_roll_summary()` / `_hard_window()` / `context_stats()`
- 交付口径：`_verify_gate_needed()` / `_should_rewrite_final()` / `_rewrite_answer()`
- 收尾：`_wrap_up_on_max_turns()` / `_max_turns_fallback()`
- 系统提示：`DEFAULT_SYSTEM_PROMPT` / `CHAT_SYSTEM_PROMPT` / `_sync_system_prompt()`
- SSE 入口：`src/core/webui.py` 的 `_handle_chat_stream()`；会话锁 `_lock_session_or_409()`
- 前端：`web/src/components/Chat.tsx`、`web/src/App.tsx`、`web/src/components/ToolChain.tsx`
- @ 引用：`web/src/components/composer/mention.ts` / `MentionMenu.tsx` / `FileRefChips.tsx` / `refDrag.ts`（文件树拖拽协议）；后端 `_handle_workspace_search` / `_handle_workspace_stat` / `_resolve_references`（`src/core/webui.py`）、`_build_user_message`（`src/core/agent.py`）

## 注意事项

- 快速对话不是「智能体减工具」，而是**不同的系统提示与不同的行为**：不发思考参数不等于不思考，所以必须显式传 `reasoning_effort: "none"` / `thinking.type=disabled` / `enable_thinking=false`。
- 工具异常不会中断循环，会以 `[error] ...` 文本还给模型。
- 同一会话串行执行；跨会话可并行。
- 历史消息只保留用户可见内容，内部注入消息（如中途递图、收尾提醒）不计入历史。
