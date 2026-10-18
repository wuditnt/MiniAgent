# Agent 内联提问卡片（ask_user）实施方案

## 一、Summary

新增 `ask_user` 工具：Agent 在对话中需要用户澄清或抉择时，**暂停当前轮**，在助手回复气泡内部直接渲染一张交互卡片（单选按钮 + 「其他」自定义输入）。用户点选或输入后即时回传，Agent 在**同一轮继续执行**，无需到输入框打字。

实现完全复用现有 `request_approval` 的成熟通道：`工具函数 → 进程级 asker → UI 桥 → SSE 事件 → Event.wait 阻塞工作线程 → POST 接口唤醒`。差异只在**呈现形态**（居中弹窗 → 气泡内联卡片）与**回答载荷**（bool → 文本）。

已确认的三项决策：
1. 交互形态：**内联卡片**（阻塞式工具，同一轮继续）
2. 回答形式：**单选 + 自定义输入**；后续扩展 **多选（收集信息）**，见第六节
3. 超时行为：**沿用现有 120s 超时**（超时/停止任务视为「用户未回答」）

## 二、Current State Analysis

现有机制（全部已核对源码）：

| 环节 | 位置 | 说明 |
| --- | --- | --- |
| 工具定义 | [approval.py](file:///d:/codeworks/mini-agent/src/core/approval.py#L70-L113) | 模块级 `_asker` 回调 + `Tool(...)` 手动构造（`exclusive=True`、`dangerous=False`） |
| asker 注入 | [agent.py#L520](file:///d:/codeworks/mini-agent/src/core/agent.py#L520)、[#L564](file:///d:/codeworks/mini-agent/src/core/agent.py#L564) | `run()` 开始时注入、`finally` 里摘除 |
| UI 桥（阻塞） | [webui.py#L352-L384](file:///d:/codeworks/mini-agent/src/core/webui.py#L352-L384) | 推 SSE → `event.wait(CONFIRM_TIMEOUT)` 挂起工作线程 |
| 唤醒通道 | [webui.py#L262-L270](file:///d:/codeworks/mini-agent/src/core/webui.py#L262-L270) + [POST /api/confirm](file:///d:/codeworks/mini-agent/src/core/webui.py#L964-L965) | `answer_confirm(cid, approve)` 置位 Event |
| 停止时解阻塞 | [webui.py#L272-L282](file:///d:/codeworks/mini-agent/src/core/webui.py#L272-L282)，调用点 [#L1933](file:///d:/codeworks/mini-agent/src/core/webui.py#L1933)、[#L2052](file:///d:/codeworks/mini-agent/src/core/webui.py#L2052) | `deny_pending_confirm()` 解除 `Event.wait` |
| 超时上限 | [webui.py#L40](file:///d:/codeworks/mini-agent/src/core/webui.py#L40) | `CONFIRM_TIMEOUT = 120` |
| 工具暴露过滤 | [webui.py#L405-L429](file:///d:/codeworks/mini-agent/src/core/webui.py#L405-L429) | `request_approval` 仅 auto 档暴露；`remember` 受记忆开关控制 |
| 注册入口 | [tools.py](file:///d:/codeworks/mini-agent/src/core/tools.py#L62-L68) | `_register_approval()` / `_register_memory()` |
| SSE 事件类型 | [types.ts#L38-L51](file:///d:/codeworks/mini-agent/web/src/types.ts#L38-L51) | 已有 `confirm_request` |
| 前端消费 | [App.tsx#L287-L314](file:///d:/codeworks/mini-agent/web/src/App.tsx#L287-L314) | `confirm_request` → `setConfirm(...)` |
| 气泡内插槽 | [Chat.tsx#L227-L297](file:///d:/codeworks/mini-agent/web/src/components/Chat.tsx#L227-L297) | `AssistantBody`：思考过程 / 工具轨迹 / Markdown 正文 |
| 消息结构 | [types.ts#L96-L110](file:///d:/codeworks/mini-agent/web/src/types.ts#L96-L110) | `ChatItem` |
| 历史持久化 | [webui.py#L241-L245](file:///d:/codeworks/mini-agent/src/core/webui.py#L241-L245) | 只存 `role/content/usage/sec/attachments`，卡片不会进历史 |

关键约束：
- `exclusive=True` 的工具在 `_run_tools` 里强制走**串行**路径（[agent.py#L1247-L1251](file:///d:/codeworks/mini-agent/src/core/agent.py#L1247-L1251)），即 `_invoke` 在主线程执行 → UI 桥不会并发弹多个卡片。`ask_user` 必须沿用这个属性。
- 答案回传后作为**工具结果**进入消息历史（模型看到的是 `ask_user` 的返回值），不额外产生一条 user 消息 —— 与 `request_approval` 语义一致。
- `/api/history` 不携带卡片数据，刷新页面后已完成 run 的卡片不会重现。

## 三、Proposed Changes

### 后端（5 个文件）

#### 1. 新建 `src/core/ask.py`

仿照 [approval.py](file:///d:/codeworks/mini-agent/src/core/approval.py) 的结构，模块级私有字段用下划线前缀：

- `_asker: Callable[[str, list[str], bool], str] | None`
- `set_asker(fn)`：注入 / 摘除询问通道
- `ask_user(question: str, options: list[str] | None = None, allow_free_text: bool = True) -> str`
  - `question` 去空白，为空则用 `"(未提供问题)"`
  - `options` 规范化：去空白、去空项、保序去重，最多保留 8 项
  - 无 asker（脚本/测试直调）→ 返回 `"[unanswered] 当前环境没有人工问答通道。"`
  - asker 抛异常 → 返回 `"[unanswered] 询问通道故障，请改用其它方式获取信息。"`
  - 用户回答为空 → 返回 `"[unanswered] 用户未作答（超时或跳过）。请基于已有信息继续，或说明你的假设。"`
  - 正常 → 返回 `"[answer] " + 用户答案`
- `register(reg=None)`：幂等注册（`target.get("ask_user") is not None` 则返回）
- `_ASK_USER_TOOL = Tool(...)`，参数 schema：

```json
{
  "type": "object",
  "properties": {
    "question": {"type": "string", "description": "要问用户的问题，一句话，具体可判断"},
    "options": {"type": "array", "items": {"type": "string"},
                "description": "可选项，2-6 个；用户也可自行输入其它答案。没有明确选项时留空"},
    "allow_free_text": {"type": "boolean", "description": "是否允许用户自由输入，默认 true"}
  },
  "required": ["question"]
}
```

`Tool` 参数：`group="核心"`、`dangerous=False`、`source="core"`、`module="core.ask"`、`exclusive=True`（必须串行，避免并发弹多张卡片）。

描述文本要点：用于「需要用户澄清/抉择才能继续」的场景；一次性把关键问题问清楚；能从上下文推断的不要问；不要用它替代正常回答。

#### 2. [src/core/tools.py](file:///d:/codeworks/mini-agent/src/core/tools.py#L62-L68)

在 `_register_memory()` 之后增加：

```python
from core.ask import register as _register_ask
_register_ask()
```

#### 3. [src/core/agent.py](file:///d:/codeworks/mini-agent/src/core/agent.py)

- 第 47 行 import 追加 `ask`：`from core import approval, ask, events, logging_setup, memory`
- `run()` 内（[#L520](file:///d:/codeworks/mini-agent/src/core/agent.py#L520) 附近）：`ask.set_asker(self._ask_user)`；`finally`（[#L564](file:///d:/codeworks/mini-agent/src/core/agent.py#L564) 附近）追加 `ask.set_asker(None)`
- 新增桥接方法（放在 `_ask_approval` 旁，[#L568-L570](file:///d:/codeworks/mini-agent/src/core/agent.py#L568-L570)）：

```python
def _ask_user(self, question: str, options: list, allow_free_text: bool) -> str:
    """ask_user 工具 → UI 的问答桥（缺方法时 _ui_call 返回 None，按未回答处理）。"""
    return str(self._ui_call("ask_user", question, options, allow_free_text) or "")
```

- [DEFAULT_SYSTEM_PROMPT](file:///d:/codeworks/mini-agent/src/core/agent.py#L70-L82) 追加一条工作原则：

> 8. 需要澄清就问：缺少关键信息或需要用户抉择时，调用 ask_user 提问（能给出候选就放进 options），不要在正文里泛泛地反问；能从上下文推断或可用默认值处理的，不要打断用户。

#### 4. [src/core/ui.py](file:///d:/codeworks/mini-agent/src/core/ui.py)

- `PlainUI` 新增 `ask_user(question, options, allow_free_text) -> str`：打印问题与编号选项，`input()` 读取；输入为数字序号时映射回选项文本；`EOFError / KeyboardInterrupt` 返回 `""`；`allow_free_text=False` 且无有效序号时返回 `""`。
- `RichUI` 覆写为带颜色的版本（同 `ask_approval` 风格，[#L291-L303](file:///d:/codeworks/mini-agent/src/core/ui.py#L291-L303)）；`console` 缺失时 `super().ask_user(...)`。

#### 5. [src/core/webui.py](file:///d:/codeworks/mini-agent/src/core/webui.py)

- `_Session.__init__`（[#L165](file:///d:/codeworks/mini-agent/src/core/webui.py#L165) 旁）新增：`self.pending_ask: tuple[str, threading.Event, dict] | None = None`
- 新增 `answer_ask(cid: str, answer: str) -> bool`，完全镜像 [`answer_confirm`](file:///d:/codeworks/mini-agent/src/core/webui.py#L262-L270)：匹配 `cid` → 写 `pending[2]["answer"] = answer` → `event.set()`
- 新增 `deny_pending_ask()`，镜像 [`deny_pending_confirm`](file:///d:/codeworks/mini-agent/src/core/webui.py#L272-L282)：写 `answer = ""` 并 `set()`，用于停止/断连
- 现有两处 `session.deny_pending_confirm()`（[#L1933](file:///d:/codeworks/mini-agent/src/core/webui.py#L1933)、[#L2052](file:///d:/codeworks/mini-agent/src/core/webui.py#L2052)）后各追加一行 `session.deny_pending_ask()`
- `WebUI` 新增方法（放在 `ask_approval` 旁，[#L367-L384](file:///d:/codeworks/mini-agent/src/core/webui.py#L367-L384)）：

```python
def ask_user(self, question: str, options: list, allow_free_text: bool = True) -> str:
    session = self.session
    cid = uuid.uuid4().hex[:12]
    event = threading.Event()
    holder = {"answer": ""}
    with session.confirm_lock:
        session.pending_ask = (cid, event, holder)
    self._push({"type": "ask_request", "id": cid, "question": question,
                "options": list(options or []), "allow_free_text": bool(allow_free_text)})
    got = event.wait(CONFIRM_TIMEOUT)
    with session.confirm_lock:
        session.pending_ask = None
    return holder["answer"] if got else ""
```

- `do_POST` 路由（[#L958-L985](file:///d:/codeworks/mini-agent/src/core/webui.py#L958-L985)）在 `/api/confirm` 之后加 `elif route == "/api/answer": self._handle_answer()`
- 新增 `_handle_answer()`，镜像 [`_handle_confirm`](file:///d:/codeworks/mini-agent/src/core/webui.py#L2029-L2041)：读 `{id, answer, session}`，`answer` 为 `str()` 后截断到 2000 字符，调用 `session.answer_ask(cid, text)`，返回 `{"ok": matched}`
- 工具暴露过滤 [`_session_registry`](file:///d:/codeworks/mini-agent/src/core/webui.py#L405-L429)：**不加过滤条件** —— `ask_user` 在 agent 模式三档确认下全部暴露（它不是安全闸，是 UX 能力）。chat 模式沿用现有规则（无工具）自然不暴露。

### 前端（5 个文件）

#### 6. [web/src/types.ts](file:///d:/codeworks/mini-agent/web/src/types.ts)

- `SSEEvent` 联合类型追加：

```ts
| { type: "ask_request"; id: string; question: string; options: string[]; allow_free_text: boolean }
```

- 新增：

```ts
/** 内联提问卡片的只读留存记录（回答后/失效后折叠展示） */
export interface AskRecord {
  question: string;
  options: string[];
  allowFreeText: boolean;
  /** 用户答案；空 = 未回答 */
  answer?: string;
  /** true = 超时/停止导致的失效 */
  expired?: boolean;
}
```

- `ChatItem` 追加 `ask?: AskRecord`
- 活跃待答状态复用 `AskRecord` 加上 `id`：新增 `export interface PendingAsk extends AskRecord { id: string }`

#### 7. [web/src/api.ts](file:///d:/codeworks/mini-agent/web/src/api.ts#L68-L79)

新增，镜像 `postConfirm`：

```ts
export async function postAnswer(id: string, answer: string, session: string): Promise<void> {
  const res = await fetch("/api/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, answer, session }),
  });
  if (!res.ok) throw new Error(await readError(res));
}
```

#### 8. [web/src/App.tsx](file:///d:/codeworks/mini-agent/web/src/App.tsx)

- 新增 state：`const [ask, setAsk] = useState<PendingAsk | null>(null);`
- SSE 分支（[#L312-L314](file:///d:/codeworks/mini-agent/web/src/App.tsx#L312-L314) 旁）新增：

```ts
case "ask_request":
  setAsk({ id: ev.id, question: ev.question, options: ev.options, allowFreeText: ev.allow_free_text });
  break;
```

- 终态清理：在 `done` / `cancelled` / `error` 分支里，若 `ask` 非空则把它折叠进对应 assistant 条目（`ask: { ...ask, expired: !ask.answer }`）并 `setAsk(null)`。实现上用一个 `settleAsk(record)` helper，避免三处重复。
- 新增回调 `handleAnswerAsk(answer: string)`：`postAnswer(ask.id, answer, session).catch(() => {})` → 把卡片折叠进 assistant 条目（`answer`）→ `setAsk(null)`
- 传给 `Chat` 组件（[#L776-L788](file:///d:/codeworks/mini-agent/web/src/App.tsx#L776-L788)）：`pendingAsk={ask}`、`onAnswerAsk={handleAnswerAsk}`

#### 9. [web/src/components/Chat.tsx](file:///d:/codeworks/mini-agent/web/src/components/Chat.tsx)

- `Props` 追加 `pendingAsk?: PendingAsk | null` 与 `onAnswerAsk?: (answer: string) => void`
- 新增内部组件 `AskCard`：
  - 活跃态：问题正文（`item.ask` 没有时才用 `pendingAsk`）+ 单选列表（`Radio.Group`，无选项时跳过）+ `allowFreeText` 时显示「其他」输入框（`Input`，选中「其他」才启用）+ 「提交」按钮（`Button`）。`Enter` 提交、`Ctrl/⌘+Enter` 同义。
  - 提交规则：选了某个选项 → 回传该选项文本；选「其他」/直接输入 → 回传输入框文本；两者都空 → 按钮 disabled。
  - 只读态（`answer` / `expired`）：折叠为一行 chip —— 已回答显示「已选择：XXX」，失效显示「未回答（已超时）」。用 `Tag`。
- 在 `AssistantBody` 里渲染（[#L279](file:///d:/codeworks/mini-agent/web/src/components/Chat.tsx#L279) 附近，Markdown 之后）：
  - `item.ask` 存在（历史/已折叠）→ 渲染只读态
  - 当前条目 `status === "streaming"` 且 `pendingAsk` 非空 → 渲染活跃态
- 传入链：`Chat` 内 `bubbleItems` 的 assistant 分支（[#L564-L585](file:///d:/codeworks/mini-agent/web/src/components/Chat.tsx#L564-L585)）把 `pendingAsk` / `onAnswerAsk` 透传给 `AssistantBody`

#### 10. [web/src/global.css](file:///d:/codeworks/mini-agent/web/src/global.css)

新增 `.ask-card` 样式：浅蓝底（与项目现有配色一致）、左侧竖线强调、圆角、与气泡正文间距；`.ask-card.expired` 转灰；chip 与选项按钮尺寸对齐现有工具栏风格。

### 文档

#### 11. [README.md](file:///d:/codeworks/mini-agent/README.md)

在「要点」列表中补充一条交互能力说明（放在三档确认模式附近）：Agent 需要澄清时会在回复气泡内给出可点选的问题卡片，支持单选 + 自定义输入，超时 120s 未答按未回答处理。这是项目约定「全部功能开发完成后必须更新 README」。

## 四、Assumptions & Decisions

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 阻塞 vs 非阻塞 | **阻塞**（同一轮继续） | 连续性好、不浪费轮次；复用已验证的 `Event.wait` 通道 |
| 答案传递方式 | **作为工具结果** 回给模型 | 与 `request_approval` 一致，不污染 user 消息历史 |
| 选项数量 | 上限 8（描述里引导 2-6） | 避免卡片过长 |
| 超时 | **120s**（沿用 `CONFIRM_TIMEOUT`） | 与危险确认行为统一，不引入新配置项 |
| 暴露档位 | agent 模式 **三档全暴露** | 它不是安全闸，是 UX 能力；与 `request_approval`（仅 auto）不同 |
| 卡片持久化 | **不写历史** | `/api/history` 结构不动，避免破坏老会话兼容 |
| 刷新页面 | 待答卡片丢失，服务端 120s 后按未回答继续 | 本次不做「断线恢复待答状态」 |
| 停止任务 | `deny_pending_ask()` 立即解阻塞，按未回答处理 | 否则工作线程会卡满 120s |

**明确不做**（避免过度设计）：断线恢复待答卡片、卡片进历史落盘、独立超时配置项。

## 五、Verification

1. **新增单元测试 `tests/test_ask.py`**（对齐 [tests/test_events.py](file:///d:/codeworks/mini-agent/tests/test_events.py) 风格）：
   - `ask_user` 无 asker 时返回 `[unanswered]`
   - 注入 asker 返回答案 → 返回 `[answer] xxx`
   - asker 返回空串 → 返回 `[unanswered]`
   - asker 抛异常 → 返回 `[unanswered]`（不抛出）
   - `options` 规范化：去空白 / 去空项 / 去重 / 截断到 8
   - `register()` 幂等（重复调用只注册一次）
   - `Tool` 属性断言：`exclusive is True`、`dangerous is False`
   - `_Session.answer_ask` 的 cid 匹配与不匹配两种情况
2. **手工验证**（后端重启 + 前端 dev）：
   - agent 模式下发一条需要澄清的任务，确认助手气泡内出现卡片
   - 点选项 → Agent 立刻继续，卡片折叠为「已选择：XXX」
   - 选「其他」输入 → 同样即时回传
   - 卡片出现时不回答、直接点停止 → 工作线程立即解除阻塞，卡片转「未回答（已超时）」，无 120s 卡顿
   - 刷新页面 → 卡片消失，服务端 120s 后继续（记录为已知限制）
   - 三档确认模式（ask / auto / full）下 `ask_user` 均可用
3. **回归**：`.venv\Scripts\python.exe -m pytest tests -q`（当前基线 14/14 全绿，不得回退）

## 六、多选支持（决策 vs 收集信息）

`ask_user` 新增布尔参数 `multi`（默认 `false`），用一个开关贯穿全链路，覆盖两种语义：

| 场景 | multi | 交互 | 语义 |
| --- | --- | --- | --- |
| 决策 / 拍板 | `false` | 单选（Radio.Group） | 从候选中选一个 |
| 收集信息 | `true` | 多选（Checkbox.Group） | 并列补充多项 |

改动清单（在原有实现上增量修改）：

- `core/ask.py`：`ask_user(question, options, allow_free_text, multi=False)`；asker 签名扩为 4 参；工具描述区分两种场景；schema 增加 `multi`
- `core/agent.py`：`_ask_user` 接收 `multi`，以**关键字** `multi=` 传给 `_ui_call`；旧签名 UI 触发 `TypeError` 回退，自动降级为单选
- `core/webui.py`：`WebUI.ask_user` 接收 `multi`，`ask_request` 事件载荷增加 `multi` 字段
- `core/ui.py`：`_resolve_answer` 支持多选解析（逗号/顿号/空格分隔，保序去重，用「、」拼回）；新增 `_ask_hint` 提示语；两个 `ask_user` 接收 `multi`
- `web/src/types.ts`：`ask_request` 事件与 `AskRecord` 增加 `multi`
- `web/src/App.tsx`：待答卡片构造与折叠留存同步保存 `multi`
- `web/src/components/Chat.tsx`：`AskCard` 按 `multi` 切换 `Radio.Group` / `Checkbox.Group`，多选结果用「、」拼接为单个字符串回传
- `web/src/global.css`：清除多选 Checkbox 的水平缩进，与单选卡片对齐

协议不变：多选结果仍以单个字符串（「、」拼接）经 `/api/answer` 回传，作为 `ask_user` 工具结果进入消息历史，无需改动后端接口与历史记录结构。
