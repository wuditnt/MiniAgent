# 技能系统

## 用途

技能是模型能**遵循**的工作流（一段指令，通常会编排多个工具），区别于模型能**调用**的工具（一次函数调用）。它解决「把领域流程固化下来，但又不长期占用上下文」。

## 概念

- **技能 = 一个目录 + `SKILL.md`**：`skills/` 为系统内置，`skills_user/` 为用户自定义。
- **清单 + 按需加载**：启动时只把 `name: description` 一行清单注入 system prompt；模型判断命中后调用 `use_skill` 把正文拉进上下文。
- **显式注入**：输入区技能选择器（或输入 `/`）插入技能块，发送时后端把正文附加到**本轮** system prompt，不写进对话历史。
- **启停状态**：`~/.mini-agent/skills_state.json` 只记录停用的技能；停用的不进清单、不出现在选择器、显式注入也忽略。
- **软删除**：目录移到 `skills_user/.trash/`，可人工恢复。

## 使用方式

### 写一个技能

新建 `skills_user/analyze_csv/SKILL.md`：

```markdown
---
name: analyze-csv
description: 分析 CSV 数据并产出可复核的数字结论
---

# CSV 数据分析流程

1. 先确认编码
2. 用 run_python 统计行数、列名、缺失值、数值分布
3. 结论附带证据（计数、聚合数字、抽样行号）
```

`name` 省略时用目录名，`description` 省略时取正文首行。frontmatter 不显示在预览里。

### 选择技能

- 输入区第三个按钮（仅智能体模式）：树形列表按「系统内置 / 自定义」分组，点一下或回车插入。
- 输入框里可放多个技能块（图标 + 名字，整块退格删除），最多 3 个、允许重复、前端不做去重。
- 输入 `/` 实时模糊过滤：忽略大小写与空格，按名称前缀 → 名称/目录名包含 → 描述包含排序；无匹配时面板隐藏，删字可恢复。
- 发送时技能名以「裸名字 + 不可见包裹字符」写进消息；后端查找、去重、注入本轮 system prompt（顺序即优先级，正文总量上限 2 万字符，超出截断）。
- 切到快速对话会移除已选技能块。

### 管理技能

左侧菜单「技能」：卡片流式布局，自定义在上、系统内置在下，卡片含图标、名称、描述、启用开关。点击查看 `SKILL.md`；自定义可原地编辑，`⋯` 菜单可「在对话框中试用 / 删除」；系统内置只读。启停 / 编辑 / 删除**仅本机可操作**（远程访问入口隐藏，后端 403）。

### 自带技能

CSV 分析、UI 渲染、文档写作、会议纪要、调研简报、日志排错、翻译润色、演示大纲。

## 关键代码位置

- 加载与状态：`src/core/skills.py` 的 `SkillLoader`（`discover()` / `load_all()` / `reload()` / `set_enabled()` / `render_index()`）、`parse_frontmatter()`、`Skill`
- `use_skill` 工具：`register_use_skill_tool()`
- 消息解析与本轮注入：`src/core/webui.py` 的 `_skills_from_message()` / `_skill_context()`；`Agent.run()` 的 `extra_system`
- 技能管理接口：`_handle_skills_list()` / `_handle_skill_detail()` / `_handle_skill_save()` / `_handle_skill_enabled()` / `_handle_skill_delete()`
- 前端：`web/src/components/SkillManager.tsx`、`web/src/components/composer/SkillPicker.tsx`、`SkillInput.tsx`、`skillIcons.tsx`
- 测试：`tests/test_skills.py`、`tests/test_skill_admin.py`

## 注意事项

- 技能正文不进对话历史，只进本轮 system prompt；因此它不会在后续轮次里重复占用上下文。
- 「允许重复」是产品选择：同一个技能块放两次不会去重，后端只按名字取一次正文。
- 停用的技能即使被显式注入也忽略。
- 技能目录里可以有辅助文件，但只有 `SKILL.md` 会被注入；需要模型读取其他文件时，在正文里写明路径。
