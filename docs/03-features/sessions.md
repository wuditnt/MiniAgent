# 会话

## 用途

多会话、持久化、搜索、标题、删除与重生成。会话是用户看到的「一条对话」，也是恢复历史的主口径。

## 概念

- **会话 JSON**：`~/.mini-agent/sessions/<sid>.json`，原子写（临时文件 + `os.replace`）。存消息、模式、模型与推理等级、工作区、标题、用量等。
- **事件流**：`<sid>.events.jsonl`，事实层，与会话双写；删除会话时一起删。见 [运行轨迹](trajectory.md)。
- **display 层**：展示用的消息列表，过滤掉内部注入消息，`tool_calls` 与工具结果按轮折叠。
- **会话锁**：同一会话同时只允许一个 run 在跑；后台运行时发新消息返回 409。

## 使用方式

| 操作 | 入口 |
|---|---|
| 新建 | 侧栏「对话 / 任务」双按钮；`Ctrl/⌘+N` |
| 搜索 | 侧栏搜索框：标题 + 全文，空格分隔多词 AND，命中片段高亮，点击滚动定位并闪烁 |
| 改名 | 侧栏会话项菜单 |
| 删除 | 会话项删除；也支持多选删除 |
| 清空 | 顶栏「⋯」菜单；`Ctrl/⌘+Shift+Delete` |
| 导出 | 顶栏「⋯」菜单，Markdown 下载；`Ctrl/⌘+E` |
| 重新生成 | 助手消息工具栏：回溯到对应提问重答 |
| 编辑重发 | 用户消息工具栏：原文回填输入框 |
| 删除此条及之后 | 用户消息工具栏 |

耗时与 token 用量显示在每轮消息下方；历史回放的消息没有用量与耗时，对应项自动隐藏。

## 标题生成

1. 新建会话时由首条消息**本地规则清洗**：剥掉口语前缀与敬语、按停顿/词边界截到 20 字、附件消息用文件名占位。
2. 首轮回答结束后，记忆提炼那次调用**顺带**产出更贴切的模型标题（同一次调用，零额外 token），后台线程写入并**锁定**。
3. 用户手动改过名的会话不会被覆盖；关掉记忆提炼或首轮过短时保留规则标题。

## 长会话

- 默认只渲染最近 30 条，向上滚动自动再加载 30 条（按高度差补偿，阅读位置不跳）；内容不足一屏时显示「加载更早的 N 条消息」按钮兜底。
- 历史气泡做了 memo，打字与流式输出不会重解析未变化的历史消息。
- 上下文层面的收敛见 [对话与智能体](chat-and-agent.md#收尾与上下文)。

## 关键代码位置

- 会话对象：`src/core/webui.py` 的 `_Session`（`ensure_agent()` / `save()` / `emit()` / `begin_run()` / `end_run()`）
- 落盘与读取：`_session_file()` / `_save_session_file()` / `_load_session_file()` / `_delete_session_file()` / `_list_session_files()`
- 接口：`_handle_session_create()` / `_handle_session_delete()` / `_handle_session_truncate()` / `_handle_session_delete_messages()` / `_handle_session_update()` / `_handle_session_model()` / `_handle_history()` / `_handle_sessions_list()`
- 会话锁：`_lock_session_or_409()`
- 标题：`_tidy_title()` / `_derive_title()`
- 搜索：`_match_terms()` / `_snippet()`
- 展示层：`_display_items()` / `_delete_display()` / `_keep_slice()` / `_renumber_runs()`
- 用量：`_usage_delta()`
- 前端：`web/src/components/Sidebar.tsx`、`web/src/components/Chat.tsx`、`web/src/App.tsx`

## 注意事项

- 会话 JSON 是恢复与搜索的主口径；事件流是事实层与兜底，两者不一致时以 JSON 为准（事件流可完整重建请求，用于排障）。
- 删除会话会同时删除 `.json` 与 `.events.jsonl`，以及该会话的主题摘要。
- 标题一旦被手动修改就不再自动覆盖。
- 会话里保存了模型与推理等级；换模型后下一次对话生效，不会改写本轮已发生的内容。
