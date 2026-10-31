# 运行轨迹（事件流）

## 用途

把每次 `run()` 的「事实」按发生顺序落盘，供排障与回放。它是会话 JSON 之外的事实层，也是「模型到底看到了什么」的唯一可校验来源。

## 概念

- **文件**：`~/.mini-agent/sessions/<会话id>.events.jsonl`，append-only，一行一个 JSON 信封。
- **信封**：`{v, seq, ts, type, source, phase, turn, run_id, payload}`。
- **12 种 `type`**：`run_start` / `system_prompt` / `user_message` / `context_inject` / `llm_request` / `llm_response` / `tool_call` / `tool_result` / `approval` / `cancel` / `error` / `run_end`。
- **`source`**：`system | user | memory | model | tool | approval | loop`，决定轨迹行着色。
- **关键不变式 I1**：`llm_request` 只存逐条消息的 `sha256` 摘要，不存正文；正文各自只存一次在 `system_prompt` / `user_message` / `llm_response` / `tool_result` 等事件里。因此事件流不膨胀，且仅凭事件流即可完整、可校验地重建每次请求的消息列表（图片以 `#img:<sha256>` 占位）。

## 使用方式

- **入口**：助手消息脚注区的时钟图标；侧栏会话项 hover 的同一图标（先切到该会话）。抽屉与右侧工作台互斥，共用宽度记忆。
- **视图**：按 `run_id → turn` 两级分组的时间线；每行显示序号 / 时间 / `type` 徽标 / 一行摘要；hover「详情」展开语法高亮的原始内容。
- **过滤**：顶部 `全部 / 模型 / 工具 / 上下文 / 授权` + 「只看本轮」。
- **回放**：每个轮次分组头的「回放」按钮按时间顺序逐条揭示（纯前端展示，不重发请求）。
- **实时**：运行中 SSE 附带 `trace` 帧即时推进抽屉，不阻塞；收到 `done / error / cancelled` 后前端再拉全量做磁盘兜底。
- **开关**：「系统配置 → 通用配置 → 运行轨迹 → 记录会话事件流」（`events_enabled`，默认开）。关闭后不再产生新文件；旧会话显示「无事件记录」而不报错。

## 与会话 JSON 的关系

- `.json` 是恢复历史、搜索、展示的主口径；
- 事件流是事实层与兜底（双写）；
- 删除会话时两个文件一起删。

## 关键代码位置

- 写入与读取：`src/core/events.py` 的 `EventWriter`、`events_path()` / `events_exist()` / `iter_events()` / `last_seq()` / `load_events()`
- 重建：`rebuild_messages()` / `rebuild_display()`、`message_digest()` / `canonical_message()`
- 循环埋点：`src/core/agent.py` 的 `_emit()` / `_emit_run_end()` / `_emit_tool_results()`；未挂 `event_writer` 时是纯 no-op
- 接口：`src/core/webui.py` 的 `_handle_trajectory()`（`GET /api/trajectory?session=&from_seq=&limit=`，默认 500、上限 5000）
- 前端：`web/src/components/Trajectory.tsx`
- 测试：`tests/test_events.py`（只追加 / 请求可重建 / 裁剪留痕 / 开关 / 兜底）

## 注意事项

- 事件流不含对话正文以外的敏感信息，但工具参数会以预览形式出现；分享前注意脱敏。
- 写入失败只吞掉，绝不影响任务。
- 事件流按 append-only 设计，不提供删除单条事件的接口。
- 裁剪上下文会在事件里留痕，回放时能看到「历史被裁过」。
