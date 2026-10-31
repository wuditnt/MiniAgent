# 流式输出 + 并行工具执行：实现报告

> 历史文档：术语按当时口径（插件 / 工具混用），正文未改写。

在 mini-agent 上补的两项能力。核心循环仍然不关心工具从哪来 ——
这两项改造没有让 `agent.py` 出现任何 `if 来源 == xxx`。

**结论先说**：全部测试通过（6 + 11 + 18 + 端到端 4 项 + 23/23 复核），
真实 DeepSeek 跑了一次，8 个工具并行批次的墙钟 **0.61s**（最长单个 0.60s，**加速 7.9x**）。

---

## 一、改了什么

| 文件 | 改动 |
|---|---|
| `agent.py` | `_request` / `_consume` / `_consume_stream` / `_run_tools` / `_prepare` / `_call_tool` |
| `ui.py` | 新增 `llm_delta` / `tools_start` / `tools_done` / `flush_stream`，全类加可重入锁 |
| `repl.py` | 新命令 `/stream` `/parallel` `/workers`；回答不再重复打印；最终回答按原样输出 |
| `main.py` | `--no-stream` / `--no-parallel` / `--workers N` |
| `core/registry.py` | `Tool.exclusive` 字段 + `@tool(exclusive=True)` |
| `src/mini_agent/core/mcp.py` | **修并发缺陷**：响应按 id 精确投递，不再互相抢 |
| `mcp/mcp_demo_server.py` | 新增 `sleep` 工具（并发测量用）+ 请求并发处理 |
| `tests/test_stream_parallel.py` | 新增，18 项 |
| `tests/test_http_e2e.py` | mock 服务支持 SSE（分片工具调用）+ 三条降级路径验证 |

---

## 二、流式：难的不是"接收"，是"拼回去"

开启后回答逐字上屏。真正会写错的地方在增量聚合：

| 字段 | 到达方式 | 写错的后果 |
|---|---|---|
| `delta.content` | 直接累加 | 回答缺字 |
| `delta.tool_calls[].id` | **只在第一片** | 覆盖会丢 → `tool_call_id` 匹配不上 |
| `delta.tool_calls[].function.name` | **只在第一片** | 覆盖会丢 → 工具名空 |
| `delta.tool_calls[].function.arguments` | **逐片累加** | 只取最后一片 → JSON 解析必然失败 |
| `usage` | 藏在 **choices 为空的纯统计帧** 里 | 漏采 → 流式模式下没有 token 统计 |

参考 `agent.py::_consume_stream`。

### 三级降级（都来自真实服务端行为）

1. **不认识 `stream_options`** → 去掉它重试。代价只是流式下没 token 统计。
2. **压根不支持 `stream`** → 退回普通模式，功能不受影响。
3. **答应了流式却一帧都不发** → 本轮改用普通模式重试。

第 3 条是最值得写的：`openai` SDK 遇到"声称 SSE 却回 JSON"的服务端时
**不会抛异常，而是静默返回空流**。如果不检测，表现就是
「Agent 一本正经地回答了空字符串」，而且**只在换网关时才出现**。
判定方式是数帧数：`_stream_seen == 0` → 可以安全重试（一帧都没收到，不会重复计费也不会丢内容）。

### 一个反直觉的判定：为什么用 `__next__` 而不是 `__iter__`

```
Stream.__next__           = True    __iter__ = True
ChatCompletion.__iter__   = True    __next__ = False   ← pydantic 模型也实现了 __iter__
```

用 `__iter__` 判断会把**普通响应误当成流**去迭代。可靠的区别是：
**流是迭代器（有 `__next__`），响应对象不是。**

### 取舍（诚实说）

| 得到 | 放弃 |
|---|---|
| 首字延迟从"整段生成完"降到"第一个 token" | 流式文本是纯文本，不能再交给 rich 重排 markdown |

想要富文本排版 → `/stream off`，回答整段返回时交给 rich 渲染（代码块、表格好看）。
另外流式文本直接写 `sys.stdout` 绕过 rich：一是模型输出里的 `[xxx]` 会被当成 rich 标记，
二是 rich 按"每次调用"排版，逐块输出会把长行切碎。

### 一个只有 UI 知道的事实

`ui.llm_delta()` **返回布尔值**（"这段我真的显示了吗"）。
因为如果 UI 被 `/verbose off` 关掉、一个字都没打，Agent 却告诉控制台"已经打过了"，
最终回答就会**凭空消失** —— 两边都以为对方打了。
`test_disabled_ui_must_not_claim_streamed` 固化了这条。

---

## 三、并行：边界划在"执行"这一步

| 阶段 | 在哪跑 | 为什么 |
|---|---|---|
| 解析参数 / 查工具 / **危险确认** | 主线程**串行** | 确认要问人，不能并发弹窗 |
| 真正的工具执行 | 线程池 | 子进程 / IO / 网络能重叠 |
| 结果回填 | 主线程按**原顺序** | 协议要求 `tool_call_id` 一一对应 |

不并行的三种情况：只有一个调用（线程池是纯开销）、关掉了、本批含 `exclusive=True` 的工具。

轨迹打印也放回主线程 —— 工作线程只返回 `(结果, 耗时)`。这样轨迹行不会互相插队。

---

## 四、抓到的问题

### ① MCP 客户端并发串话（真 bug，只在并发时复现）

```python
# 改前：从公共队列取，id 不匹配就丢弃
while True:
    msg = self._rx.get(timeout=remaining)
    if msg.get("id") != rid:
        continue          # ← A 线程会把 B 线程的响应取走并丢掉，B 只能等到超时
```

串行调用永远正确，一并发就随机超时。改成「每个在途请求一个专属队列，
`_read_stdout` 按 id 精确投递；无人认领的（通知）才进公共队列」。

### ② 空流静默失败（见上文）

### ③ `__iter__` 判定不牢（见上文）

### ④ 自己的低级错误：参数传错

`_request(client, schemas)` —— 把工具 schema 列表当成了 payload 字典。
**`tests/test_loop.py` 在第一秒就报了 `argument after ** must be a mapping, not list`。**
这就是"循环层要有假客户端测试"的价值：它不验证模型，只验证你的管道。

---

## 五、验证

### 单元测试 `tests/test_stream_parallel.py`（18 项）

```
--- 流式 ---
✓ 分片内容拼回完整回答 + 边生成边上屏 + usage 从统计帧采集
✓ UI 关闭时 last_answer_streamed=False，调用方会补印，回答不会丢
✓ 工具调用分片按 index 聚合（name 不丢、arguments 逐片拼接）
✓ 同一轮多个工具调用交错分片时，仍按 index 归位且顺序正确
✓ 推理模型的 reasoning_content 只用于展示，不进回答
✓ 客户端返回非可迭代响应时自动走普通路径，不抛异常
✓ 空流自动降级为普通模式重试（不会回一个空回答）
--- 并行 ---
✓ 3×300ms 工具 0.90s → 0.30s（3.0x），且结果顺序与 tool_calls 一致
✓ max_workers 上限生效（3 个 400ms 任务、2 线程 → 0.80s，约两批）
✓ exclusive 工具让整批退回串行
✓ 轨迹按「先播报批次、再逐个出结果、最后报墙钟」输出
✓ 单个工具抛异常 / 工具名不存在，都不影响同批其它工具与循环
✓ 危险确认在主线程串行完成，被拒的那个单独返回 [denied]，其余照常执行
✓ 工具调用计数按调用数累加（不是按批次）
--- UI 容错 ---
✓ 只实现了 0 个方法的 UI 也不会让循环崩溃
✓ 流式增量直接落 stdout，flush_stream 收尾换行且不会重复补
--- MCP 并发 ---
✓ 乱序到达的响应按 id 精确投递，通知不占用响应通道
✓ 4 个并发请求 0.25s 全部成功、各自拿到自己的结果
```

### 端到端 `tests/test_http_e2e.py`（本地 mock 服务，支持 SSE）

```
### ① 流式输出 + 并行工具（默认）        通过 ✓   6/6 请求走 SSE，usage 正确采集
### ② 服务端不认识 stream_options        通过 ✓   400 后自动去参重试，仍是流式
### ③ 网关忽略 stream 参数                通过 ✓   首个请求空流 → 第二个自动改 stream=false
### ④ 显式关闭流式与并行                  通过 ✓   传统形态未回归

### 并行 vs 串行（4 个真实 MCP 调用，各 500ms）
  串行 2.00s → 并行 0.50s（加速 4.0x）
  ✓ 4 个 tag 各自对应，无串话
```

### 真实 DeepSeek 运行（原始日志 `real_run_stream.log`，当时在项目根；现已归档）

任务：四项独立调查 + 明确要求"独立的调用同轮发出"。

```
 LLM 调用 5 次 | 工具调用 12 次 | tokens 输入 17607 + 输出 2008 = 19615
```

轨迹（批次统计是新加的）：

```
第 1 轮  ⚙ 并行执行 8 个工具（count_lines + demo__sha256 ×2 + list_dir + demo__sleep ×4）
         ⏱ 8 个工具并行完成，墙钟 0.61s（最长单个 0.60s）  加速 ≈ 7.9x
第 2 轮  ⚙ 并行执行 2 个工具（run_python ×2）
         ⏱ 2 个工具并行完成，墙钟 3.89s（最长单个 3.89s）  加速 ≈ 2.0x
第 3 轮  （单个工具 → 走串行路径，不起线程池）
```

- 4 个 `demo__sleep(600ms)` 各自 601ms，**同一批完成**，批次墙钟 0.61s —— 串行需要 2.4s。
- 8 个工具里三条通道全用上了：`count_lines`（用户插件）、`demo__sha256`（MCP）、
  `list_dir`（内置）、`demo__sleep`（MCP 慢工具）。
- 流式输出确实逐段上屏（日志第 1/28/39 行是模型在工具间隙的即时输出）。
- 最终回答**只打印一次**（`last_answer_streamed` 生效），内容与 `tests/verify_answer.py`
  的独立复核 **23/23 一致**，两个 SHA256（`5cf6a591…` / `cd7c8424…`）逐位相同。

---

## 六、怎么用

```bash
python -m mini_agent                      # 默认就是流式 + 并行
python -m mini_agent --no-stream          # 关流式（配合 rich 富文本排版）
python -m mini_agent --no-parallel        # 关并行（对照实验用）
python -m mini_agent --workers 8          # 最多 8 个工具同时跑
```

控制台里：`/stream on|off`、`/parallel on|off`、`/workers 8`。
环境变量 `MINI_AGENT_STREAM` / `MINI_AGENT_PARALLEL` / `MINI_AGENT_MAX_WORKERS`（旧名 `AGENT_*` 仍可读）。

---

## 七、下一步建议

1. **持久化记忆** —— 跨会话长期记忆与检索
2. **真正的取消** —— Ctrl+C 目前只能中断输入，掐不掉在跑的工具（并行后更需要）
3. **摘要式上下文压缩** —— 现在是"超限裁剪"，还可以把老轮次摘要成一段
4. **可观测性** —— 结构化日志 + 成本归因（批次墙钟已经埋好了）
