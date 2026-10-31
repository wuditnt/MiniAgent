# mini-agent 真实运行报告

> 历史文档：命令行入口与一次性任务模式已移除，正文中的 `python -m mini_agent …`
> 命令按当时口径保留；现在请用 `python -m core` 启动 Web UI。

> 运行时间：2026-10-14 20:03
> 模型：DeepSeek `deepseek-chat`（OpenAI 兼容接口）
> 脚本：`main.py` · 任务书：`demo_case/TASK.md`

---

## 一、运行配置

| 项 | 值 |
|---|---|
| Base URL | `https://api.deepseek.com/v1` |
| 模型 | `deepseek-chat` |
| temperature | 0.0 |
| 工作区（沙箱根） | `C:\Users\jie.dai\WorkBuddy\我的测试\mini-agent` |
| 暴露给模型的工具 | 8 个 |
| max_turns | 25 |

**执行命令**

```bash
python -m mini_agent "请阅读 demo_case/TASK.md，然后严格按里面的三项任务执行，并给出汇总表"
```

---

## 二、完整工具调用轨迹

共 **6 轮 LLM 调用**、**10 次工具调用**。注意每一轮都**并行发出 2 个工具调用**——
这是模型自主决定的并行 (parallel function calling)，不是代码里写死的。

| 轮次 | 并行工具调用 | 返回摘要 |
|---|---|---|
| 1 | `read_file(demo_case/TASK.md)`<br>`list_dir(demo_case)` | 任务书 17 行；目录 2 项 |
| 2 | `list_dir(demo_case/inbox)`<br>`run_shell(cd demo_case/inbox && ls -la && file *)` | 5 个文件；shell **exit=1**（`file` 命令缺失） |
| 3 | `read_file(demo_case/inbox/README.txt)`<br>`run_python(逐个文件读首 16 字节)` | 背景说明；拿到 5 个文件的魔数 |
| 4 | `run_python(编码判定 + sha256)`<br>`run_python(zlib 魔数 + 解压)` | GBK vs UTF-8+BOM 确认；`78 da` → 解压 1729B |
| 5 | `run_python(token 正则统计)`<br>`run_python(PNG 魔数 + NUL 统计)` | 6 个去重 token；PNG 签名 + 18 个 `0x00` |
| 6 | — | 输出最终回答（4.5s） |

### 轨迹里三个值得学的细节

**① 第 2 轮的 `run_shell` 返回了 `exit=1`，循环没有崩。**
因为 `tools.call()` 把所有异常和错误出口都转成了文本还给模型。模型看到失败后**没有重试同样的命令**，而是在第 3 轮改用 `run_python` 去读魔数——这正是"工具报错不是终点，换一种方式重试"的行为，也是工具层设计（异常 → 文本）在真实场景下生效的证据。

**② 它没有踩"假阴性"陷阱。**
本次任务里 `packet.bin` 是 zlib 压缩流，直接 grep 原始字节会得到 0 命中。模型在拿到 shell 输出的文件大小后，**直接选择了 `run_python` 解压**，并在最终回答里主动加验："实测 `b'trace'`、三个 token 的字节序列在原始 355 字节中均搜不到，全部为 `False`"。这是它自己意识到"0 结果需要追问"。

**③ 它主动加了任务书没要求的证据。**
任务书只要求"给出可复核的数字"，它额外计算了两个 CSV 的 SHA256 前 16 位（`5cf6a5916443ea25` / `cd7c84242da7ccd2`）——这与复核脚本独立算出的值**逐位相同**，说明结论不是编的。

---

## 三、Agent 的回答 vs 标准答案

用 `tests/verify_answer.py` 从磁盘产物**独立反推**标准答案（不引用生成器里的常量），逐项比对：

```
通过 23/23
结论: Agent 的报告与实测标准答案完全一致 ✅
```

| # | 检查项 | Agent 报告 | 实测 | 结果 |
|---|---|---|---|---|
| 1 | GBK 文件字节数 | 175 | 175 | ✓ |
| 2 | UTF-8 文件字节数 | 211 | 211 | ✓ |
| 3 | GBK 首 3 字节 | `d0 d5 c3` | `d0 d5 c3` | ✓ |
| 4 | UTF-8 首 3 字节 | `ef bb bf` | `ef bb bf` | ✓ |
| 5 | 文本内容相同 | True | True | ✓ |
| 6 | 非 ASCII 字符数 | 33 | 33 | ✓ |
| 7 | 体积差 | 36 B | 36 B | ✓ |
| 8 | 体积差公式 | 3 + 33×1 = 36 | 成立 | ✓ |
| 9 | packet.bin 大小 | 355 B | 355 B | ✓ |
| 10 | zlib 魔数 | `78 da` | `78 da` | ✓ |
| 11 | 解压后大小 | 1729 B | 1729 B | ✓ |
| 12 | 重复 token | 13 / 7 / 5 | 13 / 7 / 5 | ✓ |
| 13 | token 总数 / 去重 | 28 / 6 | 28 / 6 | ✓ |
| 14 | 原始字节搜不到 token | True | True | ✓ |
| 15 | 解压后可搜到 token | True | True | ✓ |
| 16 | PNG 签名 | `89 50 4e 47 0d 0a 1a 0a` | 同 | ✓ |
| 17 | PNG 的 `0x00` 个数 | 18 | 18 | ✓ |
| 18 | PNG 不可按 UTF-8 解码 | 失败 | 失败 | ✓ |

**诱饵处理正确**：只出现 1 次的 `A1B2C3D4E5F60718`、`5F6E7D8C9B0A1234`、`DEADBEEFCAFEF00D` 被明确排除，没有混进"重复 token"表。

---

## 四、成本

```
[统计] LLM 调用 6 次 | 工具调用 10 次 | tokens 输入 15460 + 输出 2050 = 17510
```

| 指标 | 数值 |
|---|---|
| LLM 往返 | 6 次 |
| 工具执行 | 10 次 |
| 输入 tokens | 15,460 |
| 输出 tokens | 2,050 |
| 合计 | **17,510** |

**值得注意的比例**：输入 15,460 : 输出 2,050 ≈ **7.5 : 1**。这解释了为什么"工具输出截断"和"上下文裁剪"是 Agent 成本的第一大闸门——
如果 `_clip` 不生效，一次 `cat` 大文件就能让单轮输入膨胀十倍，而这一轮的输入会在**后续每一轮**被重复计费。

按 DeepSeek 定价粗估，本次运行成本约 **¥0.02 量级**（输入缓存未命中价）。

---

## 五、复现步骤

```bash
cd mini-agent
pip install -r requirements.txt
python -m mini_agent --web      # 首次启动后先在「设置 → 模型」里配置模型（存 models.json）

python make_demo_case.py      # 造沙箱数据（幂等，可重复执行）
python tests/verify_answer.py       # 只看标准答案：不需要 Key
python tests/test_loop.py           # 循环层单测：不需要 Key
python tests/test_http_e2e.py       # 真实 HTTP 管线验证：不需要 Key（本地 mock 服务）

python -m mini_agent "请阅读 demo_case/TASK.md，然后严格按里面的三项任务执行，并给出汇总表"
```

---

## 六、这次真实运行验证到了什么

| 能力 | 是否被真实验证 | 证据 |
|---|---|---|
| 多轮工具调用循环 | ✅ | 6 轮、10 次调用、正确收敛 |
| 并行工具调用 | ✅ | 每轮 2 个 tool_call 同时发出 |
| 工具报错不中断 | ✅ | 第 2 轮 `exit=1` 后继续 |
| 二进制降级 | ✅ | `logo.png` 走 hex 预览而非硬解 |
| 上下文裁剪 / 输出截断 | 未触发 | 本次总量 17.5k tokens，未达 120k 阈值 |
| 沙箱越界拦截 | 未触发 | 模型全程未越界（这是好事） |
| 危险命令拦截 | 未触发 | 未尝试危险命令 |

> **这两个"未触发"本身就是信息**：防护机制在真实良性任务里不应该被触发。要验证它们，靠的是 `tests/test_loop.py` 里的构造用例，而不是等真实任务去撞。
