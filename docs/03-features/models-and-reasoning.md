# 模型与推理等级

## 用途

模型配置只管「有哪些模型可用」；具体用哪一份、思考到什么程度，由每个会话单独决定。

## 概念

- **模型配置（entry）**：服务商 + base_url + API Key + 模型名 + 显示名，存 `~/.mini-agent/models.json`。
- **会话级选择**：每个会话在输入框模型菜单里选模型与推理等级，随会话 JSON 持久化、互不影响。新会话默认模型列表第一份。
- **推理等级**：统一三档 **Low / High / Max**，默认 **High**。不同厂家用不同参数形态适配。
- **快速对话**：显式关闭思考，不显示推理等级。

## 支持的服务商

内置模板：DeepSeek（默认，模型选项 `deepseek-flash` / `deepseek-v4-pro`）、智谱、Kimi、通义千问、豆包、千帆、硅基流动、OpenAI、Ollama、vLLM。

任意 OpenAI 兼容接口都可加一份配置。

## 推理等级如何下发

| 厂家类型 | 参数形态 |
|---|---|
| DeepSeek / Kimi / 千帆 / 硅基流动 / Ollama 等 | `reasoning_effort` 直传 low / high / max |
| 智谱 GLM / 豆包 | `thinking` 开关 + `reasoning_effort`；**豆包只认 low / high**，Max 不显示 |
| 通义千问兼容模式 | `enable_thinking` + `reasoning_effort` |
| OpenAI | 无 max 档，三档重映射为 low / medium / high |
| 未声明厂家 | 按通用三档下发；接口不认思考参数时自动摘参重试一次 |

旧配置里的 `off` / `medium` 自动迁移为 `low` / `high`。

快速对话关闭思考的写法：通用风格发 `reasoning_effort: "none"`，thinking / switch 风格分别发 `thinking.type=disabled` / `enable_thinking=false`。

## 使用方式

1. 「系统配置 → 模型配置」新建一份：选模板 → 填 Key → 填模型名（显示名会自动带出，可改）。
2. 点「连接测试」：`GET /models` 探测，不耗 token；401/403 与网络错误分开报。
3. 对话输入框模型菜单：选模型 + 推理等级，只影响当前会话。
4. 顶栏点模型名也能进模型配置页。

API Key 只在本机；接口回显脱敏为 `****后四位`。

## 上下文水位

输入区上方显示上下文占用百分比（绿 → 黄 → 红），上限取「通用配置」的上下文字符上限。超限时的裁剪策略见 [对话与智能体](chat-and-agent.md#收尾与上下文)。

## 关键代码位置

- 配置读写：`src/core/model_config.py` 的 `create_entry()` / `update_entry()` / `delete_entry()` / `resolve_active()` / `resolve_model()` / `resolve_endpoint` 相关函数 / `list_public()`
- 推理等级：`normalize_reasoning()` / `reasoning_level()` / `reasoning_spec()` / `provider_key()` / `set_reasoning()`
- 连接测试：`test_entry()` / `test_values()`
- DeepSeek 联网 Key：`deepseek_key()`
- 应用参数：`src/core/agent.py` 的 `_apply_reasoning()` / `_resolve_endpoint()` / `_resolve_model()`
- 会话级切换：`src/core/webui.py` 的 `_handle_session_model()` / `_handle_model_reasoning()`
- 前端：`web/src/components/ModelSettings.tsx`、`web/src/components/Chat.tsx` 的模型菜单

## 注意事项

- 「模型配置」没有全局当前模型的概念：当前用哪份由会话决定。
- 推理等级按会话保存；切换会话会还原该会话的选择。
- 豆包不提供 Max 档，界面会隐藏该档位。
- 接口不认思考参数时会自动去掉参数重试一次，不会让整轮失败。
