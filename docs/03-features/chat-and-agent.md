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
- 「智能搜索」开关按会话独立保存，两种模式都可用。

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
- **收尾验证门**：本轮执行过写文件 / 执行命令类操作时，注入一条系统提醒，要求交付前验证（读回文件、核对数字、看完整命令输出）。
- **上下文水位**：输入区上方显示占用百分比；超长时先裁旧工具结果，再把较早整轮摘要，最后滑动窗口硬截断（保留 system + 摘要 + 最近 8 轮）。
- **任务执行归后端**：跑在服务端 worker 线程，关页面只停推送不中断执行；同一会话后台运行时发新消息返回 409。

## 关键代码位置

- 循环与取消：`src/core/agent.py` 的 `Agent.run()` / `_run_loop()` / `_request()` / `_consume_stream()` / `_run_tools()` / `interrupt()` / `_patch_cancelled_replies()`
- 上下文：`_trim_context()` / `_trim_old_tool_results()` / `_roll_summary()` / `_hard_window()` / `context_stats()`
- 收尾：`_wrap_up_on_max_turns()` / `_max_turns_fallback()` / `_verify_gate_needed()`
- 系统提示：`DEFAULT_SYSTEM_PROMPT` / `CHAT_SYSTEM_PROMPT` / `_sync_system_prompt()`
- SSE 入口：`src/core/webui.py` 的 `_handle_chat_stream()`；会话锁 `_lock_session_or_409()`
- 前端：`web/src/components/Chat.tsx`、`web/src/App.tsx`、`web/src/components/ToolChain.tsx`

## 注意事项

- 快速对话不是「智能体减工具」，而是**不同的系统提示与不同的行为**：不发思考参数不等于不思考，所以必须显式传 `reasoning_effort: "none"` / `thinking.type=disabled` / `enable_thinking=false`。
- 工具异常不会中断循环，会以 `[error] ...` 文本还给模型。
- 同一会话串行执行；跨会话可并行。
- 历史消息只保留用户可见内容，内部注入消息（如中途递图、收尾提醒）不计入历史。
