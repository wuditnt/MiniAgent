# 确认与授权

## 用途

危险操作由谁来拍板：每个都问、交给模型判断、还是沙箱内不问。确认机制解决「黑名单拦不全」与「每次都问会打断任务」之间的矛盾。

## 概念

三档确认模式（仅智能体模式，输入框工具栏盾牌胶囊切换，localStorage 记住）：

| 档位 | 行为 |
|---|---|
| **按需询问 `ask`（默认）** | 每个危险工具执行前都弹窗确认 |
| **自主判断 `auto`** | 普通危险操作不打扰；系统提示词注入责任准则，模型认为有风险时主动调 `request_approval(reason)` 向你申请 |
| **完全控制 `full`** | 沙箱内操作一律不弹 |

两条硬底线：

1. **危险的 MCP 工具在任何档位都强制弹窗**（外部能力不可信）。
2. **沙箱外写入 / 删除 / 移动一律硬禁**，没有放开开关。

## 判定「危险」

- 工具自身的 `dangerous` 标记（内置工具在定义处标注；MCP 工具默认危险）。
- `dangerous` 与 `exclusive` 是两件不同的事：前者决定要不要问，后者决定能不能并行。

## 弹窗如何工作

1. 工具执行前的危险确认永远在**主线程串行**做，不能并发弹窗。
2. 后端通过 UI 桥接请求确认，SSE 推给前端 → 前端渲染确认卡片。
3. 用户批准 / 拒绝后回传；拒绝则工具不执行，结果以拒绝说明还给模型。
4. auto 档下模型调用 `request_approval` 时，弹窗展示申请理由，批准才继续。

## 使用方式

- 输入框工具栏盾牌胶囊切换档位；选择记在 localStorage。
- 快速对话模式没有档位切换（不执行危险工具）。
- 想减少打断：用 auto，并在记忆里写清项目规矩，让模型知道什么该问。

## 关键代码位置

- 三档定义与工具：`src/core/approval.py` 的 `normalize()` / `register()` / `request_approval()` / `set_asker()`
- 判定与执行：`src/core/agent.py` 的 `_needs_confirm()` / `_prepare()` / `_execute()` / `_ask_approval()`
- 后端桥接：`src/core/webui.py` 的 `_Session.ask_confirm()` / `_Session.answer_confirm()` / `_Session.deny_pending_confirm()`、`UIWebUI.ask_confirm()` / `ask_approval()`
- 前端：`web/src/components/ConfirmPanel.tsx`、`web/src/components/Chat.tsx`
- 测试：`tests/test_webui.py` 的危险确认用例

## 注意事项

- 确认档位与「沙箱读取三档」是正交的两件事：三档回答「要不要问」，读取档回答「能不能读」。
- MCP 工具的危险标记可在 `mcp/mcp_servers.json` 里显式设 `"dangerous": false`，但默认仍然弹窗。
- 页面断开 / 用户离开时，待确认按「拒绝」处理，不无限等待。
- 确认只拦截工具执行，不改变模型对工具的理解；被拒绝后模型会看到拒绝理由并自行调整。
