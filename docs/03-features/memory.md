# 记忆系统

## 用途

让智能体跨会话记住「你是谁、项目有什么规矩、之前聊过什么」，同时不把噪声塞进上下文。

## 三层

| 层 | 位置 | 作用域 | 内容 |
|---|---|---|---|
| 用户画像 | `~/.mini-agent/memory/user_profile.md` | 跨所有工作区 | 偏好、习惯、长期事实 |
| 项目记忆 | `<工作区>/.memory/project.md` | 随项目文件夹走 | 规则、已做决定、踩过的坑 |
| 主题摘要 | `<工作区>/.memory/topics/<会话id>.md` | 单个会话滚动摘要 | 最近聊了什么、进展与结论 |

项目记忆放在工作区内的 `.memory/`，是为了随项目目录一起迁移；创建时加 Windows 隐藏属性，工作区是 git 仓库时自动写入 `.gitignore`。

## 条目分级

| 类型 | 含义 | 权重 |
|---|---|---|
| `pref` | 稳定的偏好 / 习惯 | 4 |
| `rule` | 项目规则 / 约定 | 4 |
| `decision` | 已拍板、以后照做的决定 | 3 |
| `pitfall` | 排查清楚的坑 | 2 |
| `fact` | 普通事实（一次性数据） | 1 |

- 注入时按「权重 → 新旧」重排；预算不够时先丢低价值内容。
- 项目记忆注入前做相关性筛选：规则/偏好全量保留，决定/坑/事实按 2-gram 相关度取前 8 条。
- 项目记忆容量 60 条，超出先淘汰权重最低、最旧的条目。
- 同主题 upsert：相似度 ≥ 0.88 时替换旧表述，完全重复跳过。
- 自动提炼按层级过滤：`user` 只收偏好与长期事实；`project` 只收规则/决定/坑/偏好，拒收 `fact`。

## 注入

每轮 `run()` 开头重建 system prompt：基础提示 +（auto 档）责任准则 + 记忆块。

- 用户画像全量注入；
- 项目记忆先筛选，再按预算（剩余预算的 80%）逐条保留；
- 主题摘要取「最近 3 条 + 与本轮输入关键词命中的」，总预算默认 1500 字符（可调），超限先裁摘要；
- 记忆块标注为「参考事实而非新指令」，与用户当前要求冲突时以当前要求为准。

## 四条写入通道

1. **用户显式指令（最可靠）**：消息以「请帮我记住 / 帮我记住 / 请记住 / 记住：/ 记一下：/ 记下来 / 以后都记住 / 以后请记住」开头时，后端在模型运行前直接写入用户画像。快速对话同样生效，本轮 system prompt 立刻带上。结果通过 SSE `memory` 事件提示「已记入 / 已存在 / 功能已关闭」。
2. **`remember(scope, note, kind)` 工具**：模型识别到「记住…」「以后都…」「这个项目的规矩是…」时主动写入。`scope=user` 写画像，`scope=project` 写项目记忆；`kind` 缺省 user→`pref`、project→`rule`，项目层拒收 `fact`。
3. **自动提炼**：每轮回复成功后一次非流式小请求（超时 15s，失败静默），输出 `{kind, note}` 数组；主题摘要滚动合并；寒暄与过短轮次跳过。
4. **手动**：左侧抽屉 → 工作区记忆（项目记忆 / 主题摘要）、设置 → 记忆（用户画像）。

条目格式：`- 2026-10-17 14:30 · [rule] 来源 — 正文`（旧格式无 `[kind]` 仍可读）。主题摘要每工作区保留最近 20 条。

## `.memory` 是保护区

以下四道防线共用 `memory.is_memory_path()`：

- 沙箱双通道：`safe_path` / `read_path` 命中即拒；
- 工具层：`list_dir` / `glob_files` / `grep` 不可见，`read_file` 读不到，`count_code_lines` 不计入，`zip_create` 自动排除；
- `run_python` prelude：`open`、`pathlib`、`shutil`、`sqlite3` 全部拦截；
- `run_shell`：重定向与写动词指向 `.memory` 时拒绝。

记忆模块自身读写走直连路径 + 临时文件 `os.replace` 原子写 + 进程内锁，不经过沙箱。

## 关键代码位置

- 存储与渲染：`src/core/memory.py` 的 `memory_dir()` / `project_path()` / `user_profile_path()` / `append_note()` / `render_memory_block()` / `select_project()` / `clip_entries()` / `prune_entries()`
- 保护区：`is_memory_path()`
- 工具与显式指令：`remember()` / `parse_remember_command()` / `infer_user_kind()` / `register()`
- 注入与提炼：`src/core/agent.py` 的 `_memory_block()` / `_sync_system_prompt()` / `_maybe_extract_memory()` / `_extract_notes()`
- 接口：`src/core/webui.py` 的 `_handle_memory_get()` / `_handle_memory_put()` / `_handle_memory_topic_delete()` / `_remember_from_message()`
- 前端：`web/src/components/MemoryPanel.tsx`、`WorkspaceMemoryModal.tsx`
- 测试：`tests/test_memory.py`

## 注意事项

- 删除会话会一并清理该会话的主题摘要。
- 记忆总开关关闭时，显式指令也不写入。
- 自动提炼失败静默，不影响主回答。
- 已知边界与沙箱一致：shell / python 的混淆写法无法 100% 拦截。
- 语义召回仍是字符重合与关键词计数，同义改写命中不了（见 [known-issues](../04-operations/known-issues.md)）。
