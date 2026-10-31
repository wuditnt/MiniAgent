# 联网搜索与抓取

## 用途

让模型能查实时信息、读指定网页。联网开关按会话独立保存，两种对话模式都可用。

## 概念

- **智能搜索开关**：顶栏「⋯」菜单第一项，按会话保存。开 = 提供搜索 + 云端抓取；关 = 不搜索，`fetch_url` 只做本地直连抓取。
- **搜索后端**：`search_provider`，与聊天模型解耦。`deepseek`（默认）/ `tavily` / `bocha` / `bing`。
- **DeepSeek 联网 Key**：独立于模型配置，存 `settings.json` 的 `deepseek_search_api_key`，Search API 与 Fetch API 共用。
- **两套抓取**：开联网时 `fetch_url` 优先 DeepSeek Fetch（返回干净 Markdown，按次计费），失败回退本地抓取；关联网时只走本地（剥离 HTML 标签）。

## 使用方式

1. 「系统配置 → 联网搜索」选择后端。
2. DeepSeek 后端需填联网 Key（已配置时只显示脱敏尾号，留空保存表示不修改）。
3. Tavily / 博查读环境变量 `TAVILY_API_KEY` / `BOCHA_API_KEY`。
4. 会话顶栏「⋯」菜单打开智能搜索开关。
5. 直接让模型「搜一下……」或给链接让模型读取。

改完即时生效（无缓存）。

## 工具

| 工具 | 行为 |
|---|---|
| `web_search` | 按所选后端查询，返回标题 / 链接 / 摘要 |
| `fetch_url` | 开：先 DeepSeek Fetch；失败回退本地直连。关：仅本地直连 |

工具范围：快速对话只挂少量即时工具（含联网搜索/抓取）；智能体模式在开关打开时额外加载 `web_search`。

## 关键代码位置

- 开关状态：`src/core/net_mode.py` 的 `set_mode()` / `get_mode()` / `is_cloud()` / `reset()`
- 搜索实现：`builtin_tools/web_search.py` 的 `web_search()` 与 `_BACKENDS`
- 抓取实现：`builtin_tools/net_tools.py` 的 `fetch_url`
- 后端选择：`src/core/webui.py` 的 `_search_backend()`
- 提示注入：`src/core/agent.py` 的 `_net_hint()` / `_net_reminder()`
- 配置项：`src/core/settings.py` 的 `search_provider` / `deepseek_search_api_key`
- 测试：`tests/test_web_search.py`、`tests/test_net_tools.py`

## 注意事项

- 联网开关按会话保存，切到别的会话互不影响。
- 搜索后端与聊天模型无关：任何厂商模型都能用所选搜索能力。
- DeepSeek 联网 Key 与模型 Key 完全解耦、互不引用。
- 关联网时不提供搜索，`fetch_url` 只适合读取明确给出的链接。
- 本地抓取会剥离 HTML 标签，复杂页面可能丢失结构；需要干净正文时用 DeepSeek Fetch 或人工提供内容。
