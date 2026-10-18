# P0 · 可追溯运行内核（会话事件流）实施计划

> 上游文档：[迭代路线：迈向「强大的个人工作智能助手」](file:///d:/codeworks/mini-agent/.trae/documents/roadmap-personal-assistant.md) · P0
> 目标一句话：把「模型看到的一切」写进一条 **append-only 事件流**，让每一次运行都可重建、可查看、可回放。

---

## 1. Summary

### 1.1 要解决的问题

现在的会话落盘（[webui.py](file:///d:/codeworks/mini-agent/src/core/webui.py#L218-L259)）只存 `user` / `assistant` 两种角色的展示态消息：**中间过程全部丢失** —— 模型当时看到的 system prompt（含记忆块）、每一轮真正发出去的 messages 数组、工具参数与结果、确认决策、裁剪与摘要动作，都不在任何地方留痕。文件日志（`logs/llm|tool|app`）是「给人排查用的」，不是「模型可见的事实」，无法据此重建请求。

具体不可回答的问题：

| 问题 | 现在能否回答 |
| --- | --- |
| 「这一轮模型到底看到了哪些消息？」 | ✗ |
| 「回答变差是不是因为注入了某条记忆？」 | ✗ |
| 「这次运行第 3 轮被裁剪掉了什么？」 | ✗ |
| 「这条回答花了多少 token、卡在哪一步？」 | 只能给总数（`_stats`），无法按轮/按来源拆 |
| 「回放一遍这次运行 / 从某轮分叉」 | ✗ |

### 1.2 P0 交付物

1. 新增 [`src/core/events.py`](file:///d:/codeworks/mini-agent/src/core) → `src/core/events.py`：会话事件流（JSONL，仅追加）。
2. [`agent.py`](file:///d:/codeworks/mini-agent/src/core/agent.py) 在 8 个关键点埋点，把「模型可见的事实」emit 到事件流。
3. [`webui.py`](file:///d:/codeworks/mini-agent/src/core/webui.py)：事件流写入生命周期（与既有会话 JSON **双写**）+ 新增只读接口 `GET /api/trajectory`。
4. 前端新增 **Trajectory 视图**（[`Trajectory.tsx`](file:///d:/codeworks/mini-agent/web/src/components) → `web/src/components/Trajectory.tsx`）：按来源着色、可展开、按轮分组、单轮回放。
5. 新增不变式测试 [`tests/test_events.py`](file:///d:/codeworks/mini-agent/tests) → `tests/test_events.py`，并登记进 [run_tests.py](file:///d:/codeworks/mini-agent/tests/run_tests.py#L57-L72)。

### 1.3 P0 边界（明确不做）

- **不改**沙箱、审批、工具注册表、模型适配器（那是 P1）。
- **不删**会话 JSON：它仍是 resume / 搜索 / keep 截断 / 前端展示的唯一口径（见 §7 决策 D2）。
- **不做** fork / replay 执行、不做语义检索、不做成本金额（分别在 P2/P3/P4）。

### 1.4 硬底线不变

事件流由 **会话层** 写入 `web_data/sessions/`（与既有会话文件同目录），不经过 `core.sandbox.safe_path`，也**不新增任何工具**；`approval` 事件只**记录**决策结果，[`_needs_confirm`](file:///d:/codeworks/mini-agent/src/core/agent.py#L1167-L1182) 的三档语义一行不动。因此「沙箱外一律禁写」「MCP 危险工具任何档位强制弹窗」两条底线不受影响。

---

## 2. Current State Analysis

### 2.1 会话持久化（现状）

| 位置 | 现状 |
| --- | --- |
| [`_SESSIONS_DIR`](file:///d:/codeworks/mini-agent/src/core/webui.py#L53) | `web_data/sessions/` |
| [`_save_session_file / _load_session_file / _list_session_files`](file:///d:/codeworks/mini-agent/src/core/webui.py#L512-L568) | 原子写 `{id,title,title_custom,workspace_id,messages,created_at,updated_at}`；`_list_session_files` 只读元信息 |
| [`_Session.save()`](file:///d:/codeworks/mini-agent/src/core/webui.py#L218-L259) | 把 `agent.messages[1:]` 过滤成 user/assistant，`_stats`(usage/sec) 展开为内联 `usage`/`sec` 落盘 |
| [`_Session._inject_history()`](file:///d:/codeworks/mini-agent/src/core/webui.py#L194-L216) | resume：从 JSON 读回 user/assistant，`usage/sec` 还原为 `_stats`、`attachments` 还原为 `_attachments` |
| [`_display_items()`](file:///d:/codeworks/mini-agent/src/core/webui.py#L597-L645) | 展示态归一化：把同一轮的多个 assistant 段合并成一个气泡（历史/搜索/keep 三处共用，索引口径必须一致） |
| [`_handle_history`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1055-L1072) / [`_handle_session_truncate`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1145-L1200) | 会话回放与「重新生成」截断，均基于 `agent.messages` / JSON |

**结论**：JSON 是「展示态物料」，天然丢过程。P0 不替换它，而是**并行**维护一份事实流。

### 2.2 运行时事件（现状）

- [`WebUI`](file:///d:/codeworks/mini-agent/src/core/webui.py#L284-L390) 把 Agent 的 9 个 UI 回调翻译成 10 种 SSE 事件：`notice / turn / delta / llm_done / tool_start / tool_end / confirm_request / cancelled`（+ 终态 `done / error / cancelled`，见 [`_run_worker`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1913-L1972)）。
- 下发路径：[`/api/chat` 的 SSE 泵](file:///d:/codeworks/mini-agent/src/core/webui.py#L1877-L1911) → [`_sse_frame`](file:///d:/codeworks/mini-agent/src/core/webui.py#L899-L906) → 前端 [`chatStream`](file:///d:/codeworks/mini-agent/web/src/api.ts#L366-L411) → [`App.tsx` 的 switch](file:///d:/codeworks/mini-agent/web/src/App.tsx#L277-L354)。
- **关键差异**：SSE 事件是「界面渲染指令」（`delta` 逐帧、`tool_end.result` 被 `HISTORY_RESULT_LIMIT` 截断），字段不足以重建请求；而事件流是「事实记录」（完整 system prompt、每轮 messages 摘要、裁剪动作）。两者**不能互相替代**，P0 新增的是后者。

### 2.3 可复用的既有能力（P0 的省力点）

- **循环埋点位置干净**：[`run()`](file:///d:/codeworks/mini-agent/src/core/agent.py#L398-L473) 有明确的开始/结束/取消边界；[`_run_loop`](file:///d:/codeworks/mini-agent/src/core/agent.py#L775-L886) 每轮有唯一的「构造 payload → 发请求 → 收响应 → 回填」四段；[`_run_tools`](file:///d:/codeworks/mini-agent/src/core/agent.py#L1047-L1140) 有唯一的「结果齐备」出口（两处 `return`）。全部埋点都落在**主线程**，无需为事件流加锁。
- **模型可见内容天然有界**：工具结果在进 messages 前已被 [`sandbox.clip`](file:///d:/codeworks/mini-agent/src/core/sandbox.py#L173) 按 `max_output_chars`（默认 8000）截断；system prompt 含记忆块也受 `memory_budget_chars`（默认 1500）约束。所以「记录模型看到的内容」不会让事件文件无限膨胀 —— 这是本方案敢全量记录的前提。
- **记忆注入点集中**：[`_sync_system_prompt`](file:///d:/codeworks/mini-agent/src/core/agent.py#L545-L556) 每轮重建 `messages[0]`；[`render_memory_block`](file:///d:/codeworks/mini-agent/src/core/memory.py#L294-L329) 是唯一的渲染出口。
- **上下文收敛点集中**：[`_trim_context`](file:///d:/codeworks/mini-agent/src/core/agent.py#L282-L353) 三级（工具结果占位 / 滚动摘要 / 硬截断）各是一个函数，各自是「messages 被改动」的唯一来源。
- **测试基建**：假客户端 `_StubClient`（[test_loop.py](file:///d:/codeworks/mini-agent/tests/test_loop.py#L21-L60)）可完全控制模型输出，能在零 token 下复现多轮 + 工具 + 裁剪。

### 2.4 需要新增的「可重建」缺口

`agent.messages` 在运行中并不是单纯的 append：有三类**就地改写**会让「朴素重放」失效，必须各自留下痕迹：

1. `messages[0]` 被 `_sync_system_prompt` 每轮重建（记忆块变化）；
2. `_trim_old_tool_results` 把老的 tool 消息正文替换成占位符（[L314-L324](file:///d:/codeworks/mini-agent/src/core/agent.py#L314-L324)）；
3. `_roll_summary` / `_hard_window` 整体重建 messages 列表（[L326-L353](file:///d:/codeworks/mini-agent/src/core/agent.py#L326-L353)），另外 `take_images()` 会追加一条多模态 user 消息（[L881-L883](file:///d:/codeworks/mini-agent/src/core/agent.py#L881-L883)）。

对应的记录职责，全部落在 §3.3 的 `context_inject` 事件上。

---

## 3. 事件模型设计

### 3.1 文件与信封

- **路径**：`web_data/sessions/<sid>.events.jsonl`（与 `<sid>.json` 同目录、同前缀，便于清理与会话同生命周期）。
- **格式**：JSONL，一行一个事件，**只追加**（`open(path, "a")`；不重写、不修改已写行）。
- **信封**（所有事件共有）：

```json
{
  "v": 1,
  "seq": 17,
  "ts": 1760751234.567,
  "type": "llm_request",
  "source": "model",
  "phase": "turn",
  "turn": 2,
  "run_id": "r-8f2c1a",
  "payload": { }
}
```

| 字段 | 含义 |
| --- | --- |
| `v` | 事件格式版本，便于未来演进 |
| `seq` | 会话内单调递增整数；启动时读回文件尾的 `seq` 续写，绝不回退 |
| `ts` | 事件时间（epoch 秒，`time.time()`） |
| `type` | 事件类型（§3.2 枚举） |
| `source` | 谁产生的：`system` / `user` / `memory` / `model` / `tool` / `approval` / `loop` |
| `phase` | 运行阶段：`run_start` / `turn` / `context` / `run_end`（Trajectory 着色与过滤的主维度） |
| `turn` | 轮次归属；`None` 表示跨轮（run 级） |
| `run_id` | 本次运行的标识，用于把同一会话的多次 run 分组 |
| `payload` | 类型专属字段 |

### 3.2 事件类型表

| `type` | `source` | `phase` | `payload` 关键字段 |
| --- | --- | --- | --- |
| `run_start` | loop | run_start | `model, approval, message_chars, attachment_count, attachments:[{name,path,mime,size}], tags:{}` |
| `system_prompt` | system | run_start | `content, chars, sha256, parts:{base, auto_responsibility, memory}` |
| `user_message` | user | run_start | `content, chars, sha256, attachments:[{name,path,mime,size}]` |
| `llm_request` | model | turn | `model, temperature, tools:[name…], message_count, chars, system_sha256, messages:[{role, sha256, chars, kind}]` |
| `llm_response` | model | turn | `sec, streamed, usage:{prompt_tokens,completion_tokens,total_tokens}, content_chars, content_sha256, head, tail, tool_calls:[{id,name}]` |
| `tool_call` | tool | turn | `id, name, args, args_chars, dangerous, exclusive, confirm:"ask"/"skip"/"mcp_hard"` |
| `tool_result` | tool | turn | `id, name, ok, sec, chars, sha256, head, tail` |
| `approval` | approval | turn | `id, tool, decision:"allow"/"deny", mode, via:"ui"`, 以及 `reason`（`request_approval` 时） |
| `context_inject` | memory / loop | context | `kind` 见 §3.3 |
| `cancel` | loop | run_end | `reason, stage:"llm"/"tools"/"stream"/"confirm"` |
| `error` | loop | run_end | `kind: ExceptionType, message` |
| `run_end` | loop | run_end | `status:"done"/"cancelled"/"error", sec, usage_delta:{llm_calls,tool_calls,prompt_tokens,completion_tokens,total_tokens}, answer_chars` |

说明：`run_start` / `run_end` 是路线图事件清单之外补的两个**书签**事件 —— 没有它们，Trajectory 无法把一次 run 与上一次切开，也无法按 run 统计耗时与用量增量（两个字段都是 [`run()`](file:///d:/codeworks/mini-agent/src/core/agent.py#L398-L473) 与 [`_run_worker`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1913-L1972) 已经算好的现成数字）。

### 3.3 `context_inject` 的四种 `kind`（重建的关键）

| `kind` | 触发点 | payload | 为什么必须记 |
| --- | --- | --- | --- |
| `memory` | [`_memory_block`](file:///d:/codeworks/mini-agent/src/core/agent.py#L529-L543) | `chars, layers:{user_chars, project_chars, topic_count}` | 轻量占位：全文已在 `system_prompt` 事件里；这里回答「这条记忆是本轮新注入的吗」 |
| `trim_tool_results` | [`_trim_old_tool_results`](file:///d:/codeworks/mini-agent/src/core/agent.py#L314-L324) | `count, ids:[tool_call_id…]` | tool 消息正文被换成固定占位符；记录 id 即可精确还原 |
| `summary` | [`_roll_summary`](file:///d:/codeworks/mini-agent/src/core/agent.py#L326-L343) | `content, prev_chars, replaced_messages, boundary` | messages 被整体重建（`[0] + 摘要 + 尾部`）；`content` 就是新的 `messages[1]` |
| `hard_window` | [`_hard_window`](file:///d:/codeworks/mini-agent/src/core/agent.py#L345-L353) | `dropped_messages, boundary, prefix_end` | 硬截断直接丢中间段 |
| `attachment_images` | [L881-L883](file:///d:/codeworks/mini-agent/src/core/agent.py#L881-L883) | `count, mime_types` | `take_images()` 追加的多模态 user 消息（data_url 不回填正文，只记数量） |

### 3.4 不变式

- **I1（请求可重建）**：对事件流中任一条 `llm_request`，存在纯函数 `events.rebuild_messages(sid, seq)`，仅用事件流自身数据（`system_prompt` / `user_message` / `llm_response` / `tool_result` / `context_inject`）按 `seq` 顺序重放，得到当时发送的 messages 列表，且其逐条 `sha256` 与 `llm_request.messages[].sha256` **完全一致**。不读会话 JSON、不读工作区文件。
- **I2（只追加）**：已写入的事件行永不被修改或删除（压缩属未来阶段，见 D4）。
- **I3（及时落盘）**：`run_end` 写入后事件文件立即 `flush`；进程被杀最多丢失当前 run 未落盘的尾部事件，已完成的 run 完整可读。

**I1 为什么能成立、又为什么便宜**：`llm_request` **不存 messages 正文**，只存逐条 `sha256`；正文各存一次在自己的来源事件里（`user_message.content` / `tool_result.head+tail` / `llm_response.head+tail` / `context_inject.summary.content`）。因为正文受既有 `max_output_chars`、`memory_budget_chars`、`HISTORY_RESULT_LIMIT` 约束（见 §2.3），单条事件是 KB 级。

### 3.5 大小控制

- `events_value_limit`（默认 65536）：任一**字符串值**超限时，改写为 `{"__truncated__": true, "len": N, "sha256": "...", "head": "...", "tail": "..."}` —— 截断是**显式且有标记**的，不存在静默丢失；`sha256` 保证 I1 校验仍然严格（正文超限时校验降级为「哈希 + 头尾」一致）。
- `tool_result` 一律存 `head`(2KB) + `tail`(2KB) + `sha256` + `chars`，因为全文恒定可由 `sha256` 校验、且已受 8000 字符上限约束（无需存全文，Trajectory 展开时如需正文可回落到会话 JSON）。

---

## 4. Backend Changes

### 4.1 新增 `src/core/events.py`

模块职责：**只负责写与读**，不认识 Agent（`agent.py` 只依赖它的两个函数），因此可独立测试。

```python
# 事件类型与来源常量（供 agent.py / webui.py 引用，杜绝字面量拼写漂移）
RUN_START, RUN_END, SYSTEM_PROMPT, USER_MESSAGE, CONTEXT_INJECT = ...
LLM_REQUEST, LLM_RESPONSE, TOOL_CALL, TOOL_RESULT, APPROVAL, CANCEL, ERROR = ...
SRC_SYSTEM, SRC_USER, SRC_MEMORY, SRC_MODEL, SRC_TOOL, SRC_APPROVAL, SRC_LOOP = ...

def events_path(sid: str) -> str          # web_data/sessions/<sid>.events.jsonl
def sha256_text(text: str) -> str         # 规范化（ensure_ascii=False）后哈希
def message_digest(m: dict) -> dict       # {"role":…, "sha256":…, "chars":…, "kind":"tool_call"/"image"…}
def iter_events(sid: str)                 # 生成器：逐行 json.loads，坏行跳过（不炸）
def last_seq(sid: str) -> int             # 启动时读回尾部 seq
def rebuild_messages(sid: str, upto_seq: int) -> list[dict]   # I1 的实现
def rebuild_display(sid: str) -> list[dict]                   # 兜底：等价于 _display_items(json)

class EventWriter:
    """一次 run 内的事件写入器；agent 挂在实例上，run 结束关闭。"""
    def __init__(self, sid: str, *, enabled: bool = True,
                 value_limit: int = 65536, mirror=None): ...
    def emit(self, type: str, *, source: str, phase: str = "", turn=None,
             **payload) -> dict | None      # enabled=False 时返回 None（零副作用）
    def set_run(self, run_id: str) -> None  # run 边界
    def close(self) -> None
```

要点：
- `emit` 内做 `_shrink`（§3.5）、分配 `seq`、补 `ts`/`v`/`run_id`，然后 `json.dumps(ensure_ascii=False)` 追加一行并 `flush`。
- `mirror` 是可选回调（`webui` 传入，把事件即时塞进 SSE 队列做实时轨迹）；为 `None` 时完全不影响主流程。回调异常一律吞掉。
- `rebuild_messages` 的重放规则（I1 的具体算法）：
  1. 从最近一条 `system_prompt`（`sha256` 匹配该 request 的 `system_sha256`）取 `messages[0]`；
  2. 顺序追加 `user_message`；`attachment_images` 追加多模态 user；
  3. `llm_response` → 追加 assistant（`content` + `tool_calls`）；`tool_result` → 追加 tool；
  4. `summary` → 执行「`[0] + 摘要 + 尾部`」重建；`hard_window` → 删除区间；`trim_tool_results` → 按 id 替换占位符；
  5. 输出前逐条比对 `sha256`（不一致即视为不变式被破坏，测试直接失败）。

### 4.2 `agent.py` 埋点（8 个点，全部在主线程）

新增成员：`self.event_writer: EventWriter | None = None`、`self._run_id`、`self._system_sha`，以及薄封装 `self._emit(type, *, source, phase, turn, **kw)`（writer 为 None 时直接返回，保证 CLI / repl / 既有 13 套测试**零成本、零行为变化**）。

| # | 位置 | 事件 |
| --- | --- | --- |
| 1 | [`run()`](file:///d:/codeworks/mini-agent/src/core/agent.py#L418-L437) 开头（`_sync_system_prompt` 之前） | `run_start`（含 `attachments` 元数据，不含 data_url） |
| 2 | [`_sync_system_prompt`](file:///d:/codeworks/mini-agent/src/core/agent.py#L545-L556) | `system_prompt`（**仅当 content 的 `sha256` 变化时** emit；并记下 `self._system_sha`）+ `context_inject{kind:"memory"}` |
| 3 | [`_run_loop`](file:///d:/codeworks/mini-agent/src/core/agent.py#L790-L802) 构造 payload 后 | `llm_request`（`messages` 用 `message_digest`，含 `system_sha256`） |
| 4 | [`_run_loop`](file:///d:/codeworks/mini-agent/src/core/agent.py#L837-L854) 收到响应后 | `llm_response`（`usage`、`sec`、`streamed`、`head/tail`、`tool_calls`） |
| 5 | [`_prepare`](file:///d:/codeworks/mini-agent/src/core/agent.py#L1142-L1165) 解析完参数、查表之后 | `tool_call`；若走了确认分支，紧接着 `approval`（`allow`/`deny`） |
| 6 | [`_run_tools`](file:///d:/codeworks/mini-agent/src/core/agent.py#L1082-L1140) 的两处 `return` 之前 | `tool_result`（统一出口，覆盖 preset / 串行 / 并行三条路径） |
| 7 | [`_trim_context`](file:///d:/codeworks/mini-agent/src/core/agent.py#L282-L353) 三级各自内部 | `context_inject{trim_tool_results / summary / hard_window}` |
| 8 | [`run()`](file:///d:/codeworks/mini-agent/src/core/agent.py#L438-L473) 的 return / `except Cancelled` / `except KeyboardInterrupt` | `run_end{status}`；取消路径先 `cancel` |
| 8b | [`_run_loop`](file:///d:/codeworks/mini-agent/src/core/agent.py#L881-L883) 追加图片消息处 | `context_inject{kind:"attachment_images"}` |

约束：
- **不改**循环的控制流、返回值、异常语义；埋点全部是「记录」而非「分支」。
- `tool_call` / `approval` 只在主线程 emit（`_prepare` 本就在主线程串行，并行只发生在执行阶段），故无需锁。
- `error` 事件不在这里发：Agent 不吞一般异常，由 `_run_worker` 的 `except` 负责（见 4.3）。
- 埋点失败不得影响任务：`_emit` 内部整体 `try/except` 吞掉（与 `_ui_call` 同等级别的容错约定）。

### 4.3 `webui.py`

1. **新增会话级事件常量与路径助手**：`_session_events_file(sid)`（放在 [`_session_file`](file:///d:/codeworks/mini-agent/src/core/webui.py#L512-L513) 旁）。
2. **writer 生命周期**放在 [`_run_worker`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1913-L1972)：

```python
writer = events.EventWriter(
    session.id,
    enabled=bool(settings.get("events_enabled")),
    value_limit=int(settings.get("events_value_limit") or 65536),
    mirror=lambda ev: q.put({"type": "trace", **ev}),   # 可选实时轨迹
)
agent.event_writer = writer
writer.set_run(f"r-{uuid.uuid4().hex[:6]}")
try:
    ... agent.run(...)
finally:
    agent.event_writer = None
    writer.close()
```

   位置对照：[L1936-L1942](file:///d:/codeworks/mini-agent/src/core/webui.py#L1936-L1942) 之前建 writer、`try` 块（[L1933](file:///d:/codeworks/mini-agent/src/core/webui.py#L1933)-1972）内沿用、`finally` 关闭。
3. **`except Exception` 分支**（[L1969-L1972](file:///d:/codeworks/mini-agent/src/core/webui.py#L1969-L1972)）补一条 `error` 事件；`except Cancelled`（[L1963-L1968](file:///d:/codeworks/mini-agent/src/core/webui.py#L1963-L1968)）补 `cancel` + `run_end{status:"cancelled"}`（若 agent 侧尚未发）。
4. **`session.save()` 不变**（[L1953](file:///d:/codeworks/mini-agent/src/core/webui.py#L1953)）—— 双写，JSON 仍是主口径。
5. **新增只读接口** `GET /api/trajectory`：

```
GET /api/trajectory?session=<sid>&from_seq=<int>&limit=<int>
→ {"session": sid, "events": [ {v,seq,ts,type,source,phase,turn,run_id,payload}, … ], "last_seq": N}
```

   注册点：[do_GET 路由表](file:///d:/codeworks/mini-agent/src/core/webui.py#L914-L944) 增加一个分支，与 `_handle_history` 并列。`limit` 默认 500、上限 5000；无事件文件的会话返回 `events: []`（前端显示「P0 之前的历史会话」）。**只读、不写盘**，与硬底线无关。
6. **会话删除**：[`_handle_session_delete`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1119-L1143) 增加删除 `*.events.jsonl` 的一步（可选，失败不阻断 —— 与主题摘要处理一致）。

### 4.4 `settings.py`

- 新增 3 项（[DEFAULTS](file:///d:/codeworks/mini-agent/src/core/settings.py#L24-L47) / [RANGES](file:///d:/codeworks/mini-agent/src/core/settings.py#L50-L59) / [BOOL_KEYS](file:///d:/codeworks/mini-agent/src/core/settings.py#L65-L66) / [INT_KEYS](file:///d:/codeworks/mini-agent/src/core/settings.py#L62-L63) 各按需登记）：

| key | 默认 | 说明 |
| --- | --- | --- |
| `events_enabled` | `true` | 事件流总开关（关闭 = 不写文件、不影响循环） |
| `events_value_limit` | `65536` | 单值截断上限（范围 4096 ~ 1_048_576） |
| `events_keep_runs` | `0` | 预留：每会话保留最近 N 次 run 的完整事件（0 = 不裁剪） |

- 这是路线图 DoD 要求的「新增能力有独立可回滚开关」。

### 4.5 迁移与兼容

| 场景 | 处理 |
| --- | --- |
| 新会话 | 首次 `run_start` 时文件不存在 → 自动创建；`seq` 从 1 起 |
| 旧会话（P0 之前） | 无事件文件；Trajectory 显示提示，`/api/trajectory` 返回空数组，不报错 |
| 进程重启后继续同一会话 | `EventWriter` 启动时 `last_seq()` 续写，`seq` 不重复 |
| JSON 损坏/缺失 | `events.rebuild_display(sid)` 兜底恢复展示态（仅新增能力，不改 `_inject_history` 主路径） |
| 可选回填 | `python -m core.events backfill <sid>`：从旧 JSON 生成**最小事件流**（`user_message` / `llm_response` 的近似事件，`source` 标记 `backfill`），Trajectory 上明确标注「历史重建，过程缺失」。列为**可选**，不阻塞 P0 验收 |

---

## 5. Frontend Changes

### 5.1 `web/src/types.ts`

在 [`SSEEvent`](file:///d:/codeworks/mini-agent/web/src/types.ts#L39-L49) 旁新增（`trace` 作为可选成员加入联合类型；`Trajectory.tsx` 只在收到它时被激活）：

```ts
export type TraceSource = "system" | "user" | "memory" | "model" | "tool" | "approval" | "loop";

export interface TraceEvent {
  v: number; seq: number; ts: number;
  type: string; source: TraceSource; phase: string;
  turn: number | null; run_id: string;
  payload: Record<string, unknown>;
}

export interface TrajectoryData { session: string; events: TraceEvent[]; last_seq: number; }
```

### 5.2 `web/src/api.ts`

新增 `fetchTrajectory(session: string, fromSeq = 0, limit = 500): Promise<TrajectoryData>`，与 [`fetchHistory`](file:///d:/codeworks/mini-agent/web/src/api.ts#L43) 同构。

### 5.3 新增 `web/src/components/Trajectory.tsx`

- **结构**：按 `run_id` → `turn` 两级分组的时间线；每行显示 `seq`、时间、`type` 徽标、一行摘要（如 `read_file {"path":…}` / `usage 1.2k→340 · 1.8s`）。
- **着色**（`source` 决定色调，落在同一色阶的不同明度上，避免高饱和撞色）：`system` 灰、`user` 蓝、`memory` 紫、`model` 绿、`tool` 橙、`approval` 红、`loop` 中性。
- **交互**（对齐既有偏好：简洁、hover 触发、无多余图标）：
  - 行默认折叠，hover 该行时出现展开按钮；展开后用 [`CodeBlock`](file:///d:/codeworks/mini-agent/web/src/components/CodeBlock.tsx) 渲染 JSON / 代码 / system prompt（**原始换行不转义**，与确认弹窗同一约定）；
  - 顶部一行过滤器：`all / model / tool / context / approval`（按 `type` 归类）+ 「只看本轮」；
  - **单轮回放**：每个 `turn` 分组头有「回放」按钮 → 逐条按时间顺序展开该轮事件（前端定时器，纯展示，不重发请求）。
- **数据来源**：打开时 `fetchTrajectory`；运行中若收到 `trace` SSE 帧则增量 append（收到 `done/error/cancelled` 后再拉一次全量兜底，保证与磁盘一致）。

### 5.4 入口与集成

- 会话工具栏（[`ToolChain.tsx`](file:///d:/codeworks/mini-agent/web/src/components/ToolChain.tsx) 所在的消息脚注区）新增「轨迹」入口，hover 触发下拉（沿用既有复制按钮的模式），打开右侧抽屉（复用 [`Workbench.tsx`](file:///d:/codeworks/mini-agent/web/src/components/Workbench.tsx) 的抽屉骨架与状态机）。
- 侧栏会话项 hover 增加「回放」按钮（[`Sidebar.tsx`](file:///d:/codeworks/mini-agent/web/src/components/Sidebar.tsx)），与既有会话删除/重命名同组；**远程访问模式下的隐藏规则沿用既有约定**（仅隐藏工作区管理类入口，会话功能保持可用）。
- 样式追加到 `global.css`，配色沿用现有 token（聊天区白底、抽屉浅灰渐变）。

---

## 6. Tests

### 6.1 新增 `tests/test_events.py`（免费用例，假客户端）

沿用 [`bootstrap.py`](file:///d:/codeworks/mini-agent/tests/bootstrap.py) + [`_StubClient` 模式](file:///d:/codeworks/mini-agent/tests/test_loop.py#L21-L60)。

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | `test_append_only_and_seq` | 多次 `emit` 后文件行数增长、`seq` 严格递增；重开 `EventWriter` 续写不覆盖、不重号 |
| 2 | `test_rebuild_invariant`（**P0 核心**） | 2 轮含工具的脚本跑完后，对每条 `llm_request` 调 `rebuild_messages(sid, seq)`，逐条 `sha256` 与事件里的 `messages[].sha256` 一致 |
| 3 | `test_context_inject_recorded` | 构造超限上下文触发 `trim_tool_results` + `summary`（stub 摘要）+ `hard_window`，三类事件齐备，且 `test_rebuild_invariant` 仍然成立 |
| 4 | `test_tool_and_approval_events` | `ask` 档 + dangerous 工具 + 拒绝 → 事件序列含 `tool_call` → `approval(deny)` → `tool_result(ok=false)` |
| 5 | `test_cancel_event` | 令牌取消 → `cancel` + `run_end{status:"cancelled"}`；消息补齐行为不变 |
| 6 | `test_events_disabled_noop` | `events_enabled=false` → 不生成文件，`agent.messages` 与不开事件流时**逐条相等**（回归保护） |
| 7 | `test_truncation_marked` | 超 `value_limit` 的字符串被标记截断，`len`/`sha256`/`head`/`tail` 正确 |
| 8 | `test_rebuild_display_fallback` | 删掉会话 JSON 后 `rebuild_display(sid)` 与 `_display_items(json)` 结果一致 |

### 6.2 既有套件增补

- [`tests/test_webui.py`](file:///d:/codeworks/mini-agent/tests/test_webui.py)：新增 `/api/trajectory` 断言 —— 200、`events` 非空、包含 `llm_request` 与 `run_end`；无事件文件的会话返回空数组不报 404。
- [`tests/run_tests.py`](file:///d:/codeworks/mini-agent/tests/run_tests.py#L57-L72)：`SUITES` 增加 `("test_events.py", "事件流 8 项：只追加 / 请求可重建 / 裁剪留痕 / 开关 / 兜底", False)`，套件数 13 → 14。

### 6.3 手工验证脚本（真实模型，少量 token）

一次「读目录 → 确认危险操作 → 取消」的运行，随后：`GET /api/trajectory` 能看到 `system_prompt → user_message → context_inject(memory) → llm_request → tool_call → approval → tool_result → llm_request → llm_response → cancel → run_end` 的完整链路。

---

## 7. 决策与假设

- **D1 · 事件流只记录「模型可见的事实」**，不记录工具原始 stdout。原因：模型可见内容受既有截断约束（§2.3），天然有界；原始输出属文件日志职责。这条界定了事件流的体积上限。
- **D2 · 保留会话 JSON 为 resume 主路径，事件流为事实层**。路线图原文是「resume 由事件流重建」，此处**降级为两阶段**：P0 让 `rebuild_messages/rebuild_display` 先存在并受测试保护（作为 JSON 损坏时的兜底与未来 fork 的基础），`_inject_history` 暂不切换。理由：`_display_items` / `_keep_slice` / 搜索命中索引三处的口径必须严格一致，直接切换 resume 会同时改动「历史展示、重新生成、搜索定位」三条既有链路，回归面过大且与 P0「可追溯」目标无关。切换请放到 P0 之后单独评估。
- **D3 · `llm_request` 存摘要而非正文**（§3.4）。理由：正文若同时存在于 `llm_request` 与来源事件，同一轮历史会被重复写 N 份（N = 后续轮数），是 O(轮数²) 的体积放大。
- **D4 · P0 不做压缩**。既有截断已保证单次 run 体量可控（§2.3）；`events_keep_runs` 作为预留配置先落地但不启用，压缩策略等真实数据出现后再定（拱手交给后续阶段）。
- **D5 · 事件只由会话层写入、不经过沙箱**。写入方是 `webui._run_worker` / `EventWriter`，落点 `web_data/sessions/`（既有目录），不新增工具、不扩大任何工具的写权限 —— 硬底线①不受影响。
- **D6 · `approval` 事件只记录结果**，`_needs_confirm`（[L1167-L1182](file:///d:/codeworks/mini-agent/src/core/agent.py#L1167-L1182)）语义不变，MCP 危险工具在 `full` 档仍强制弹窗 —— 硬底线②不受影响。
- **D7 · 埋点零侵入**：`_emit` 在 `event_writer is None` 时是纯 no-op，确保 CLI、repl、既有 14 个套件的行为与耗时完全不变（由用例 6 保护）。
- **D8 · 实时镜像为可选**。`mirror` 回调让运行中也能看到轨迹，但**不阻塞**：它异常吞掉、且前端始终以 `done` 后拉取的全量为准（避免磁盘与内存不一致）。

---

## 8. 实施顺序

1. **地基**：`core/events.py` + `settings.py` 三个开关 + `tests/test_events.py`（用例 1/6/7）。此时未接入 Agent，零回归风险。
2. **循环埋点**：`agent.py` 按 §4.2 顺序逐个加（run 边界 → system_prompt → llm 两事件 → tool 三事件 → context_inject → run_end），每加一组补对应用例（2/4/5）。
3. **Web 层**：`webui.py` 的 writer 生命周期 + `/api/trajectory` + `test_webui.py` 断言；此步起轨迹可在浏览器里看到。
4. **前端**：`types.ts` → `api.ts` → `Trajectory.tsx` → 入口接线（§5.4）。
5. **收尾**：`README.md` 增「可追溯运行 / 轨迹视图 / `/api/trajectory` / 事件流开关」章节；全量回归。

## 9. Verification

- **不变式**：`python tests/run_tests.py events` → 8/8 通过（其中 `test_rebuild_invariant` 即 M1 判定标准）。
- **回归**：`python tests/run_tests.py` → 14/14 全绿（含原 13 套件），确认 D7「零侵入」成立。
- **真实性**：§6.3 的真实模型运行后，Trajectory 视图能按来源着色展示全链路，且单轮回放可用；刷新页面后轨迹仍在（已被 `/api/trajectory` 落盘）。
- **旧数据**：P0 之前的会话打开轨迹不报错、给出「无事件记录」提示；会话列表/搜索/重新生成等功能与 P0 前完全一致。
- **开关回滚**：把 `events_enabled` 关掉 → 不再产生文件、界面轨迹入口显示「未开启」，循环行为与 P0 前逐条一致。
- **硬底线复检**：跑 `test_memory` / `test_workbench` / `test_cancel` / `test_webui` 确认沙箱与审批行为未变；并人工确认 `full` 档下 MCP 危险工具仍弹窗、`approval` 事件记录的 `mode=full` 与弹窗事实一致。
