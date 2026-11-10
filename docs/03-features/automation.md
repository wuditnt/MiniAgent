# 自动化任务（定时执行）

## 用途

让智能体按固定节奏自动干活：到点后把一条预设消息发给指定工作区的会话，由 Agent 在后台自主执行，结果留在会话里等你看。适合「每天早上一份 AI 新闻简报」「每周一一篇竞品追踪」「每 30 分钟扫一次提交记录」这类重复性任务。

## 概念

- **任务（task）**：一条自动化配置，包含名称、执行内容（即发给模型的提示词）、工作区、模型、联网开关、时间规则与启用状态。存于 SQLite，不散落在会话文件里。
- **执行记录（run）**：每次触发产生一条，状态为 `running / ok / error / cancelled / skipped / aborted`。忙碌跳过记 `skipped`，进程异常退出遗留的 `running` 在下次启动时改记为 `aborted`。
- **绑定会话**：每个任务**首次执行**时才创建会话，此后所有周期都追加到同一个会话主题下；会话文件里带 `source=automation` 与 `automation_id`。删任务不删会话。
- **时间规则**：四种形态——`once`（单次）、`daily`（每天 HH:MM）、`weekly`（每周某几天 HH:MM）、`interval`（每 N 分钟，最小 5 分钟）。全部按本机时区计算。
- **无人值守**：自动化触发的运行没有人在屏幕前，确认请求一律自动拒绝，`ask_user` 返回「当前为自动执行，无人应答」并继续跑。
- **未读**：自动化跑完而你没有正在看那个会话时，会话被标记未读；侧栏「自动化」入口与对应会话项显示红点，打开会话即清除。
- **实时呈现**：前端常驻一条全局事件流（`GET /api/events`），只广播 `session_created` / `run_started` / `run_finished` 三个控制信号，不含正文。点「立即执行」时会话立刻出现在侧栏、运行中状态立刻点亮、跑完立刻补拉终稿；正文仍由 `/api/chat/stream` 的多会话快照 + 增量推送。`/api/running` 轮询降级为兜底。

## 使用方式

- **入口**：侧栏「自动化」。空状态给出 7 个内置模板（每日 AI 新闻简报、品牌舆情监控周报、每周竞品动态追踪、股价监控与预警（A 股）、安全漏洞扫描、提交后缺陷检查与修复、每日变更摘要），点模板即预填创建表单；已有任务后模板入口收进标题栏的「从模板新建」下拉。
- **新建 / 编辑**：填写名称与执行内容，选工作区、模型、是否联网；选重复方式并设置时间，表单下方实时预览「接下来 3 次」。任务一旦产生会话，工作区不可再改。
- **列表操作**：启停开关、立即执行（不改排期）、查看会话、编辑、删除。删除只清任务与执行历史，会话保留。
- **AI 助手**：编辑已有任务时可展开右侧助手，用自然语言说「改成工作日早上八点半」「提示词里加上只列重点」，模型返回结构化 patch，面板逐字段展示「旧值 → 新值」，确认后才写入表单，**保存仍需点保存**；对话历史保留最近 40 条，可清空。
- **远程访问**：只读。列表、详情与 AI 对话可看，新建 / 修改 / 删除 / 立即执行一律 403。
- **后台运行**：应用关闭期间错过的排期**不补跑**，启动时直接把排期推进到下一个未来时刻并累计 `missed`；单次任务过期则自动停用。同时最多跑 2 个自动化任务，超出或会话正忙时本次跳过（记 `skipped`），不排队。

## 关键代码位置

- 存储与规则：`src/core/automation.py`
  - 建表与连接：`_SCHEMA`、`_ensure_schema()`、`_conn()`、`SCHEMA_VERSION`
  - 规则：`normalize_schedule()`、`next_after()`、`next_runs()`、`describe_schedule()`、`MIN_INTERVAL_MINUTES`
  - 领取与收口：`claim_scheduled()`、`claim_manual()`、`finish_run()`、`running_runs()`、`recover_stale_runs()`、`advance_missed()`
  - 保留：`RUN_KEEP`（100 条）、`prune_runs()`、`prune_chat()`、`CHAT_KEEP`（40 条）
  - 对话历史：`list_chat()` / `append_chat()` / `clear_chat()`
- 调度：`src/core/scheduler.py`
  - `AutomationScheduler`：`start()`（先恢复再启动线程）、`_tick()`（10s，信号量限流）、`run_now()`、`_execute()`、`_mark_unread()`
- 执行链路：`src/core/webui.py`
  - `WebServer.start_run()`（会话运行链路统一入口，`/api/chat` 与自动化共用）
  - `create_automation_session()`、`run_automation_task()`
  - 无人值守短路：`WebUI` 内的 `ask_confirm` / `ask_approval` / `ask_user`
  - 全局事件流：`_emit_global()`、`_GLOBAL_SUBS`、`_handle_events()`（`GET /api/events`）
  - 本轮用户消息补齐：`_Session.pending_turn`、`_with_pending_user()`——消息真正写入 `agent.messages` 之前，快照 / 历史也能带上这一轮的用户气泡
  - 路由：`GET /api/automations`、`POST /api/automations`、`PUT|DELETE /api/automations/<id>`、`POST /api/automations/<id>/run`、`POST /api/automations/<id>/assist`、`GET|DELETE /api/automations/<id>/chat`、`POST /api/sessions/<id>/read`
  - 处理器：`_handle_automations_list()`、`_handle_automation_create/update/delete/run()`、`_handle_automation_assist()`、`_clean_automation_patch()`、`_parse_json_object()`
- 前端：`web/src/components/AutomationsPage.tsx`、`AutomationEditor.tsx`、`AutomationAssistant.tsx`、`automationTemplates.ts`；事件流在 `web/src/eventStream.ts`（全局控制信号）与 `web/src/sessionStream.ts`（多会话正文）；样式在 `web/src/global.css`
- 测试：`tests/test_automation.py`（14 项：规则边界 / 校验 / CRUD / 原子领取 / 收口 / 崩溃恢复 / 错过排期 / 对话裁剪 / 无人值守 / 未读）

## 注意事项

- **依赖应用常驻**：调度器随 Web 服务启动，进程退出后不再执行，也没有系统级计划任务或开机自启；适合本机长期开着的场景。
- **不补跑**：关机 / 休眠期间的触发点全部跳过，只在任务上累计 `missed`，不会追着补跑。
- **无人值守是自动拒绝**：需要人工确认的危险操作在自动化里会直接拒绝，可能导致该轮结果不完整；涉及敏感操作的任务建议把提示词写得更明确，避免走到确认分支。
- **会话忙碌即跳过**：同一会话正在跑（你手动发消息或上一轮还没结束）时，本次触发记 `skipped` 且不排队，下一周期照常。
- **工作区锁定**：任务一旦产生会话，切换工作区会让历史消息与新目录错配，因此后端拒绝修改。
- **数据库位置**：`~/.mini-agent/automations.db`（`automation.DB_FILE_NAME`），随会话 / 记忆一起属于本机数据；删除该文件等于清空所有自动化任务与执行历史，会话仍在。
- **AI 助手不自动保存**：patch 只回填表单，未点保存不落库；模型给不出合法 JSON 时重试一次，仍失败则返回错误提示而不改表单。
