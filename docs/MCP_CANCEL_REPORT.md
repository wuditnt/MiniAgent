# 取消通知补全报告

> 历史文档：术语按当时口径（插件 / 工具混用），正文未改写。

> 目标：把「取消」从**单方面停止等待**补成**双向事件** —— 本端收手的同时，
> 让正在为你干活的对端也停下来，并把「为什么取消」一路带到用户面前。
>
> 范围：`src/mini_agent/core/mcp.py`（客户端）、`mcp/mcp_demo_server.py`（服务端）、`src/mini_agent/core/cancel.py`（原因）、
> `ui.py` / `agent.py` / `repl.py` / `main.py`（告知用户）、新增两个测试文件。

---

## 1. 问题：取消原来只做了一半

上一轮实现的是「真正的取消」的前半段 —— 本端不再等。它的表现是这样：

```
用户 Ctrl+C
   └─► 令牌置位 → 子进程被杀 / HTTP 客户端被关 / 消息序列补齐 → run() 抛 Cancelled
   └─► 控制台打印「已取消」
```

对**本端**来说这已经完整了：程序立刻回到提示符，什么都不会卡住。
但对 MCP 这类**跨进程**工具，还差一件：对端从头到尾不知道你走了。

| 后果 | 具体表现 |
|---|---|
| 白跑 | 服务端的 `sleep(60)` 工具照跑满 60 秒，白占 CPU |
| 白占 | 连接、线程、服务端配额、外部 API 额度继续被消耗（很多服务按调用计费） |
| 状态不一致 | 服务端可能在取消后又写了文件 / 又发了一次请求 |
| 无法归因 | 服务端日志里看不到「客户端不要了」，排查时只能猜 |

MCP 协议早就为此留了位置：`notifications/cancelled`。规范原文（2024-11-05）：

```
notifications/cancelled
  params: { requestId, reason? }
  · 接收方 SHOULD 停止处理该请求
  · 接收方 SHOULD NOT 再回响应
  · 通知本身不需要响应 —— 它是一次单向表态，不是请求
```

「客户端不再等某个响应时可以用它」——包括**取消**与**超时**两种情形。

---

## 2. 改了什么

### 2.1 客户端（`src/mini_agent/core/mcp.py`）

```python
def notify_cancelled(self, rid: int, reason: str = "") -> bool:
    with self._send_lock:
        if rid in self._cancelled_ids:      # 幂等：同一 id 只发一次
            return False
        self._cancelled_ids.append(rid)     # 先登记、后发送（下面的竞态全靠它）
    try:
        self.notify("notifications/cancelled", {"requestId": rid, "reason": reason})
    except Exception:
        return False                        # 管道断了 → 通知已无意义，不上抛
    self.stats["cancellations"] += 1
    return True
```

配套的四处接线：

1. **等待循环的取消分支**：`tok.check()` 抛 `Cancelled` 之前，先
   `notify_cancelled(rid, token.reason)`；
2. **超时分支**：`reason = "客户端等待 tools/call 超时（20.0s）…"` ——
   超时同样属于「我不再等这个响应了」，不发通知服务端会为一个没人要的结果跑到底；
3. **迟到响应**（`_dispatch`）：响应到达时 waiter 已经没了。此时若 `rid` 在
   `_cancelled_ids` 里，记为 `late_responses` 并**丢弃** —— 关键是绝不能塞回
   公共队列 `_rx`，那会把一条迟到的工具结果伪装成「server 主动发来的消息」；
4. **连接关闭**（`stop()` / `_read_stdout` 结束）：`_fail_pending()` 把在途请求
   全部用一条错误帧唤醒。不做这一步的表现是「关掉 MCP server 之后程序卡住 30 秒」。

有两个顺序问题需要刻意安排，写反了就会出随机 bug：

```
① 先登记 waiter / inflight，再发请求
   —— 反过来的话响应可能在登记前到达，那一帧就成了无人认领的孤儿。

② notify_cancelled 先写 _cancelled_ids，再发通知帧
   —— 反过来的话，响应可能恰好在「发通知」与「登记」之间到达，
       _dispatch 看不到这个 id 已被放弃，就会把它丢进 _rx 污染消息通道。
```

### 2.2 服务端（`mcp/mcp_demo_server.py`）

协议只负责**送达**；「停不停手」是服务端的实现。所以同步改了自带 server：

```python
_INFLIGHT: dict[int, dict] = {}     # requestId -> {method, event, reason}（带锁）

def _check_cancelled():             # 长耗时工具在循环里调用它
    rid = getattr(_TLS, "rid", None)          # 每个请求一个线程 → thread-local 传 rid
    rec = _INFLIGHT.get(rid)
    if rec and rec["event"].is_set():
        raise RequestCancelled(...)           # → 由 _handle_safe 捕获 → **不回响应**
```

为什么不用「主线程收到通知就杀掉工作线程」？Python 没有安全的线程强杀
（`PyThreadState_SetAsyncExc` 会留下写了一半的状态）。唯一正确的做法是协作式：
通知到达 → 置位事件 → 工具在**可以安全收手**的位置自己退出。

三个新增/改造的工具：

| 工具 | 用途 |
|---|---|
| `sleep`（改造） | 每 ≤50ms 检查一次取消事件 → 收到通知就提前返回（不睡满） |
| `stubborn_sleep`（新增） | **故意无视**取消通知，睡满后照样回响应 —— 用来验证客户端的迟到响应处理 |
| `cancel_log`（新增） | 回显收到过的取消通知与在途请求 —— 让「通知是否真的送达」可被核对 |

`tools/call` 里 `RequestCancelled` 必须**先于** `except Exception` 被重新抛出，
否则会被兜底分支吞掉并回一条响应 —— 违反「被取消的请求不回响应」。

### 2.3 原因（reason）贯通到人

取消不该只说「停了」，还要说「为什么停」。加了 `CancellationToken.reason`，
一路透传：

```
agent.interrupt("用户不耐烦，按下了 Ctrl+C")
   └─► token.cancel(reason)                     ← 第一次生效，幂等不覆盖
        ├─► MCP 帧 reason: "…；客户端已中止等待 tools/call 的响应"
        │      └─► 服务端日志（对端可读，排查有据）
        └─► ui.cancelled(reason)                ← 控制台：⏹ 已取消：用户不耐烦…
               └─► agent.last_cancel_notice_shown ← 调用方据此避免重复打印
```

`ui.cancelled(reason) -> bool` 沿用已有的 `llm_delta -> last_answer_streamed`
约定：**UI 打了就不重复打**，repl / main 只在 UI 没打时补一句。
REPL 里额外显示本轮真的补发了几条通知：

```
⏹ 已取消：用户不耐烦，按下了 Ctrl+C
  已向 MCP server 补发 1 条取消通知（对端不会再把已放弃的请求跑到底）
```

`/mcp` 也加了可观测性一行：`协议调用 N 次 | 取消通知 N 条 | 超时 N 次 | 丢弃迟到响应 N 条`。

---

## 3. 验证

### 3.1 免费用例：`tests/test_mcp_cancel.py`（8/8）

```
✓ 通知送达：0.47s 内本端收手，server 收到 requestId=2（reason 已带过去）并提前停手、不回响应
✓ 迟到响应：客户端在 0.31s 放弃，对端 1.5s 后才回的那条被静默丢弃，同连接后续调用正常
✓ 超时通知：0.80s 判定超时并补发通知（reason 含『超时』），对端随即停手
✓ 幂等：同一 requestId 只发一条通知，对端的迟到通知被安全忽略
✓ 定向取消：超时的 A（requestId=2）被单独取消，并发中的 B 正常返回（通知按 requestId 精确投递，不是一刀切）
✓ 关闭连接：在途请求在 0.01s 内收到明确错误（而不是干等 30s 超时）
✓ 回归：无取消时一条通知都不发，协议调用与工具结果一切照旧
✓ 全链路：注册表路径下 0.47s 收场，通知已送达（reason 含本地原因）

全部 8 项通过 ✓
```

要点在两个「反例」上：**迟到响应**（对端不听话时客户端必须扛得住）
与**定向取消**（通知按 `requestId` 精确投递，不是「一刀切关掉所有在途请求」）。

### 3.2 真实调用：`tests/test_real_mcp_cancel.py`（11/11，消耗约 3.9k tokens）

链路：真实 DeepSeek 流式 → 模型决定调用 `demo__sleep(ms=8000)` →
stdio 发给子进程 server → 工具跑到 1.2s 时触发取消。

```
[第 1 轮] → 调用 demo__sleep(ms=8000, tag=real-cancel)
>>> demo__sleep 已执行 1.2s，触发取消（等价 Ctrl+C）<<<
         ← 返回 30 字符: [cancelled] 用户取消了本次任务，该工具未完成执行
[UI 取消通知] 真实场景：用户不耐烦，按下了 Ctrl+C
⏹ 已取消：真实场景：用户不耐烦，按下了 Ctrl+C

--- 验证点 ---
  ✓ run() 抛出 Cancelled（工具本要跑 8s）  —  3.01s
  ✓ Cancelled 文案带本地原因
  ✓ UI 收到取消提示（含原因）
  ✓ agent 记录了提示已显示
  ✓ 客户端已补发 1 条取消通知  —  {"requests": 3, "cancellations": 1, "late_responses": 0, "timeouts": 0}
  ✓ 没有丢弃不到的迟到响应
  ✓ 服务端收到通知且 reason 已转述
      — [{"requestId": 3, "reason": "真实场景：用户不耐烦，按下了 Ctrl+C；客户端已中止等待 tools/call 的响应"}]
  ✓ 服务端提前收手且不回响应
      — [demo-mcp] 收到取消通知 requestId=3 reason='…' → 已让在途工具停手
        [demo-mcp] 请求已取消，提前收手且不回响应：请求 3 已被客户端取消（…）
  ✓ 1 个 tool_call 全部有回复
  ✓ 回复里明确写了被取消
  ✓ 取消后的新对话正常完成

结果: 全部通过 ✓
```

三个数字读法：
`latency` —— 工具本要跑 8s，链路 3.01s 收场（含 1 次 LLM 往返与 1.2s 的刻意等待）；
`cancellations=1` —— 通知发出去了；
`late_responses=0` —— 服务端确实没再回响应（不是「回了但我们丢了」）。

### 3.3 回归（全部免费用例）

```
tests/test_loop.py           6/6    PASS
tests/test_plugins.py       11/11   PASS   （demo server 工具数 4 → 6）
tests/test_stream_parallel.py 19/19 PASS
tests/test_cancel.py         9/9    PASS   （MCP 用例已升级为「断言通知送达」）
tests/test_mcp_cancel.py     8/8    PASS   ← 新增
tests/test_http_e2e.py       4 场景  PASS   （SSE mock 四遍：正常 / stream_options / 空流 / 全同步）
tests/verify_answer.py      23/23   PASS
```

---

## 4. 还剩什么

- **服务端 → 客户端的取消**：客户端目前不处理 `server→client` 请求（sampling /
  roots），所以也没有「拒绝或取消它们」的语义。要做得先有那条通道。
- **通知不保证送达**：MCP 的通知是单向的，没有 ACK。写失败、对端已退出时通知就丢了。
  真正需要「一定停手」的场景得靠服务端自己的租约/超时兜底 —— 本项目里
  `mcp/mcp_servers.json` 的 `timeout` 就是最后一道闸。
- **纯 Python 阻塞调用仍无法强杀**（Python 限制）：只能靠工具作者在循环里
  `tok.check()`。`src/mini_agent/core/cancel.py` 已经把这条写进模块文档。
