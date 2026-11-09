# @ 命令（文件引用与即时上传）方案与实施计划

> 状态：方案已评审确认，**尚未实施**。
> 基线：`1a0aaa5`。
> 目标：输入框内以 `@` 唤起文件选择，可引用当前工作区文件作为上下文，也可从本地选择图片/文件即时上传；引用随消息发送，历史可回看。

## 0. 已确认决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 内容注入 | 混合模式：小文本（≤ 8KB）内联全文；大文本给路径 + 少量摘要；图片走多模态；二进制仅路径 |
| D2 | 工作区搜索 | 服务端递归搜索，按文件名 + 相对路径模糊匹配；跳过 `.git`/`node_modules` 等目录；单条消息最多 5 个引用 |
| D3 | 本地文件 | 复用现有上传链路（单文件 24MB、图片多模态、文本 8000 字节选）；支持多选、拖拽、粘贴截图；任意二进制只作附件不解析 |
| D4 | 触发规则 | 任意空白之后的 `@` 触发；`/` 继续只做技能；两个菜单互斥 |
| D5 | 引用生命周期 | 发送时记录快照元数据；文件后续变更/删除只标注，不自动重读 |
| D6 | 权限 | 远程只读模式禁止上传与新增引用，历史引用可查看；统一使用浏览器文件选择器 |
| D7 | 自动化任务 | P0 不支持保存 `@` 引用；P1 增加「任务固定引用文件清单」 |
| D8 | 数量与预算 | 每条消息引用 ≤ 5 个、附件 ≤ 5 个；内联总预算 32KB，超出自动降级为路径引用 |
| D9 | 文件落盘 | 本地文件复制进工作区 `.attachments/`；工作区引用**不复制**，发送时按路径读取快照 |

## 1. 目标与非目标

### 1.1 目标

- 输入 `@` 弹出分组面板：工作区文件、最近使用、从本地上传
- 选中工作区文件后以 chip 形式留在输入框，可整块删除
- 发送时把引用内容按规则注入模型上下文，气泡与历史中可点开预览
- 本地文件选择后立即上传，带上传状态与失败提示
- 历史中标注引用文件「已变更 / 已删除」

### 1.2 非目标

- 目录整体引用
- 工作区外的路径引用（统一走本地上传）
- 引用内容的实时跟随（每次打开重读）
- 自动化任务提示词内保存引用（P1）
- 向量检索 / 语义搜索（P0 只做名称与路径匹配）

## 2. 现有能力与复用点

| 能力 | 现状 | 复用方式 |
|---|---|---|
| 上传 | `/api/upload` 接收 base64，写入 `<workspace>/.attachments/`，上限 24MB | 前端改为多选，逐个调用，返回元数据不变 |
| 附件注入 | `_resolve_attachments` + `agent._build_user_message`：图片 `data_url`、文本 `excerpt` 8000 字 | 扩展出 references 分支，复用同样的消息构造 |
| 历史回放 | 消息 `_attachments` 元数据 + `AttachmentPreview` 组件 | 新增 `_references` 与 chip 组件 |
| 输入框 | `SkillInput` contenteditable + 私有标记渲染不可编辑胶囊 | 扩展 token 类型，同时渲染技能块与文件块 |
| 触发检测 | `slashCommands.ts` 的「插入单字符 + 边界判定」 | 新增 `mention.ts`，同一套判定思路 |
| 工作区文件 | `/api/workspace/dir`、`/file`、`/raw`、`/download` | 预览与下载直接复用；新增搜索与批量 stat |

## 3. 总体设计

```
输入框
  │ 用户键入 "@"（行首或空白后）
  ▼
MentionMenu（分组）
  ├─ 工作区文件：GET /api/workspace/search?q=  （150ms 防抖）
  ├─ 最近使用：localStorage（按工作区隔离）
  └─ 从本地上传…：<input type=file multiple> → POST /api/upload（逐个）
  │ 选中
  ▼
输入框内文件 chip（私有标记）＋ selectedRefs 状态
  │ 发送
  ▼
POST /api/chat { message, attachments, references:[{path,mtime,size}] }
  │
  ▼
服务端 _resolve_references
  ├─ 路径安全校验（realpath 必须落在工作区内）
  ├─ stat 对比客户端 mtime/size → changed 标记
  ├─ 判定 mode：inline（≤8KB 文本）/ image（≤24MB）/ path
  └─ 32KB 总预算内降级
  │
  ▼
agent._build_user_message：内容注入 + _references 元数据落盘
  │
  ▼
前端气泡：引用 chip（点击用现有预览接口打开），历史加载时批量 stat 标注变更
```

## 4. 输入框交互

### 4.1 触发规则

- `@` 必须位于开头或空白之后；紧跟文字（如 `abc@`）按普通字符处理
- 查询词截止到下一个空白或换行
- 打开面板时若技能面板可见 → 关闭技能面板；反之亦然
- Esc 关闭面板；↑↓ 移动；Enter / Tab 选中；点击选中
- 选中后删除「@ + 查询词」，插入文件 token 并补一个空格

### 4.2 面板结构

| 分组 | 内容 | 说明 |
|---|---|---|
| 工作区文件 | 搜索结果（最多 20 条） | 显示文件名 + 相对目录 + 大小；无结果时提示 |
| 最近使用 | 该工作区最近选过的 8 个文件 | localStorage 持久化，按工作区 key 隔离 |
| 操作 | 「从本地上传…」 | 打开多选文件对话框，上传完成后作为附件加入待发送区 |

查询为空时只展示「最近使用 + 从本地上传」；输入查询后才请求搜索。

### 4.3 Chip 与 token 编码

沿用 `skillMarks.ts` 的思路，新增一套互不冲突的标记：

- 输入框内部：`\u0003file:<encodeURIComponent(path)>\u0003`
- 渲染：文件图标 + 文件名（悬停显示相对路径）
- `parseComposer` 同步解析 `files`；发送时 token 从正文剥离，引用走独立字段
- 编辑重发：根据消息的 `_references` 还原 token，保持可编辑

### 4.4 附件与引用计数

- 引用最多 5 个；重复路径自动去重
- 本地附件最多 5 个；上传中展示进度态，失败可单独移除
- 引用与附件合计不设额外上限，但服务端内联预算固定 32KB

## 5. 工作区文件搜索

### 5.1 接口

```
GET /api/workspace/search?workspace_id=<id>&q=<keyword>&limit=20
→ { "items": [{ "name", "path", "size", "mtime", "ext" }],
    "truncated": false, "took_ms": 12 }
```

### 5.2 遍历与忽略

- 从工作区根递归遍历，`os.walk` + 目录剪枝
- 跳过目录：`.git`、`node_modules`、`.venv`、`venv`、`dist`、`build`、`out`、`__pycache__`、`.mypy_cache`、`.pytest_cache`、`.idea`、`.vscode`、`.attachments`、`.memory`
- 跳过所有以 `.` 开头的目录
- 防护：不跟随指向工作区外的符号链接
- 上限：访问条目 20000、深度 12、结果 30、耗时 1.5s；超限返回 `truncated: true`

### 5.3 匹配与排序

- 统一小写后匹配：文件名前缀 > 文件名子串 > 路径子串 > 文件名子序列
- 同分按 `mtime` 倒序
- 响应路径统一为正斜杠相对路径（Windows 兼容）

## 6. 引用解析与内容注入

### 6.1 请求契约

```json
"references": [
  { "path": "docs/03-features/chat-and-agent.md", "mtime": 1762700000, "size": 8421 },
  { "path": "assets/logo.svg", "mtime": 1762700000, "size": 2048 }
]
```

客户端传 `mtime/size` 仅作变更提示；服务端一律重新 `stat` 并以真实内容为准。

### 6.2 服务端解析（`_resolve_references`）

1. 路径规范化：拒绝 `..`、绝对路径、盘符；`realpath` 必须位于工作区根内，否则 400
2. 去重：与附件、彼此之间按 realpath 去重；超过 5 个直接 400
3. 读取 `mtime/size` 与客户端值对比，不一致则 `changed: true`
4. 判定注入模式：

| 类型 | 条件 | 处理 | mode |
|---|---|---|---|
| 文本 | mime `text/*`、`application/json`，或扩展名在白名单（md/txt/py/ts/tsx/js/json/yaml/yml/toml/ini/csv/log/sql/sh/html/css/xml 等） | 文件 ≤ 8KB → 全文；> 8KB → 前 40 行摘要 | `inline` |
| 图片 | `image/*` 且 ≤ 24MB | 读为 `data_url`，走视觉输入 | `image` |
| 其他 | 二进制 / 超大图片 / 超预算文本 | 仅路径注记，提示可用文件工具读取 | `path` |

5. 总内联预算 32KB：按选择顺序累加，超出后的文件降级为 `path`
6. 生成元数据：

```json
{
  "name": "chat-and-agent.md",
  "path": "docs/03-features/chat-and-agent.md",
  "mime": "text/markdown",
  "size": 8421,
  "mtime": 1762700000,
  "mode": "inline",
  "changed": false
}
```

### 6.3 注入方式

扩展 `agent._build_user_message(user_input, attachments, references)`：

- `inline` / `image`：与现有附件一致，追加 `[引用文件 <path> 内容]` 或图片分片
- `path`：追加 `[引用工作区文件 <path>（N 字节，未内联，可用文件工具读取）]`
- 引用元数据写入消息 `_references`（`_` 前缀不发给模型）

### 6.4 变更检测接口

```
POST /api/workspace/stat
{ "workspace_id": "...", "paths": ["a.md", "b.png"] }
→ { "items": [{ "path", "exists", "size", "mtime" }] }
```

- 单次最多 50 个路径，用于历史加载时批量标注「已变更 / 已删除」
- 历史 chip 状态只影响展示，不阻断重新发送

## 7. 本地文件即时上传

- 输入框「从本地上传…」、拖拽到输入区、粘贴剪贴板图片三种入口
- 统一 `<input type="file" multiple>`；逐个 `POST /api/upload`，独立展示上传中/失败
- 上传成功即进入待发送附件区（现有 chip 样式），发送时随 `attachments` 提交
- 严格沿用现有约束：单文件 ≤ 24MB；文件名清洗 + UUID 前缀；只写工作区 `.attachments/`
- 未选择工作区时提示先选择/创建，避免写入错误目录

## 8. 数据与接口

### 8.1 `/api/chat` 变更

- 新增可选字段 `references: [{path, mtime, size}]`
- 校验：最多 5 个；路径非法 400；远程只读 403
- 调度器（自动化任务）不传 references

### 8.2 消息元数据

- 用户消息新增 `_references`；会话 JSON 加载与历史接口透出（与 `_attachments` 同一处理路径）
- 历史回放：`ChatItem.references`；气泡下方渲染引用 chip

### 8.3 新增接口汇总

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/workspace/search` | 工作区文件模糊搜索 |
| POST | `/api/workspace/stat` | 批量查询文件是否存在及 mtime/size |
| POST | `/api/upload` | 已有接口，前端改为多选调用 |

## 9. 前端改动

| 文件 | 改动 |
|---|---|
| `web/src/components/composer/mention.ts`（新增） | `@` 触发检测、查询词提取、token 编解码、插入/删除 |
| `web/src/components/composer/MentionMenu.tsx`（新增） | 分组面板、键盘导航、搜索防抖、最近使用、上传入口 |
| `web/src/components/composer/SkillInput.tsx` | token 正则扩展为「技能 + 文件」两类，渲染文件 chip |
| `web/src/components/Chat.tsx` | 维护 selectedRefs、mention 面板状态；多选上传、拖拽、粘贴；发送时传 references |
| `web/src/components/FileRefChips.tsx`（新增） | 气泡与历史中的引用 chip（图标、路径、变更/删除标记） |
| `web/src/api.ts` | `searchWorkspaceFiles`、`statWorkspaceFiles`、`uploadFile` 支持多选调用 |
| `web/src/types.ts` | `WorkspaceRef`、`ChatItem.references` |
| `web/src/global.css` | 菜单分组、文件 chip、变更标记样式 |
| `web/src/App.tsx` | `send` 增加 references 参数，乐观气泡带上引用，请求体透传 |

## 10. 关键代码位置（基线 `1a0aaa5`）

| 关注点 | 位置 |
|---|---|
| 输入框 token 渲染 | `web/src/components/composer/SkillInput.tsx`（19–78） |
| 技能 token 编解码 | `web/src/components/composer/skillMarks.ts`（16–51） |
| 触发边界判定 | `web/src/components/composer/slashCommands.ts`（32–58） |
| 发送与上传 | `web/src/components/Chat.tsx`（1393–1408 上传、1882–1887 发送） |
| 前端发送链路 | `web/src/App.tsx`（871–950） |
| 上传接口 | `src/core/webui.py` `_handle_upload`（4074–4118） |
| 目录浏览 | `src/core/webui.py` `_handle_workspace_dir`（4161） |
| 附件解析 | `src/core/webui.py` `_resolve_attachments`（1131） |
| 消息构造 | `src/core/agent.py` `_build_user_message`（1456–1491） |
| 会话历史透出附件 | `src/core/webui.py`（467–490、1753–1755） |
| 附件展示组件 | `web/src/components/Chat.tsx` `AttachmentPreview`（708–730） |

## 11. 边界与风险

| 风险 | 处理 |
|---|---|
| 大仓库搜索慢 | 目录剪枝 + 访问/深度/结果/耗时上限 + 前端 150ms 防抖 |
| 上下文膨胀 | 单文件 8KB 内联、总预算 32KB、超出降级路径、引用 ≤ 5 |
| 路径穿越 | 规范化 + realpath 根目录校验；拒绝绝对路径与 `..` |
| 引用已变更 | 发送时 `changed` 标记；历史批量 stat 标注，不自动重读 |
| Windows 路径 | 服务端统一正斜杠；客户端展示原样、发送前规范化 |
| 重复引用 | 与附件按 realpath 去重 |
| 远程泄露 | 远程只读：禁止上传与新增引用；仅可查看历史 |
| 与 `/` 冲突 | 两套 token 前缀不同；面板互斥 |
| 二进制误读 | 只按 mime/扩展名白名单读文本；其余仅路径 |
| 上传失败 | 逐文件独立状态，失败不移除已成功项 |

## 12. 实施计划

### M1 后端

1. `_handle_workspace_search`：遍历、剪枝、匹配排序、上限与 `truncated`
2. `_handle_workspace_stat`：批量 stat，最多 50 条
3. `_resolve_references`：路径安全、去重、mode 判定、预算降级、`changed`
4. `agent._build_user_message` 支持 references；`agent.run` 透传
5. `_handle_chat` 解析 / 校验 references；消息 `_references` 落盘与历史回放
6. 远程只读拦截；自动化调度入口不传 references
7. `tests/test_mention.py`：搜索、路径校验、注入模式、预算、变更标记、远程 403

**验收**：用测试 + 临时 HTTP 请求完成——搜索命中并跳过忽略目录、非法路径 400、文本内联、大文件降级、图片 data_url、历史回放带 `_references`。

### M2 输入框

1. `mention.ts`：触发检测、查询词、token 编解码
2. `MentionMenu.tsx`：分组、搜索防抖、最近使用、上传入口
3. `SkillInput.tsx`：双 token 渲染与序列化
4. `Chat.tsx`：selectedRefs、面板互斥、键盘操作、多选/拖拽/粘贴上传
5. `FileRefChips.tsx` + 历史变更标注（批量 stat）
6. `App.tsx` / `api.ts` / `types.ts` / `global.css`

**验收**：`cd web && npm run build` 通过；开发环境手工完成「@ 搜索选文件 → chip → 发送 → 气泡显示引用 → 点击预览」「本地上传图片 → 发送 → 多模态可见」。

### M3 联调与文档

1. 端到端：引用 5 个文件、超出降级、文件发送后修改/删除的历史标注
2. 回归：`pytest tests -q` 全绿；`/` 技能命令、附件上传、SSE 无回归
3. 文档：更新 `docs/03-features/chat-and-agent.md`（输入框能力一节），必要时在 `docs/README.md` 补索引

### 后续（不在本期）

- 自动化任务固定引用清单（`tasks.references_json`）
- 目录引用、引用内容实时跟随
- 语义/向量检索、引用排序打分可视化
- 引用文件全文搜索命中片段高亮

## 13. 测试计划

### 单元

- 触发检测：行首 / 空白后触发；`abc@` 不触发；查询词截止到空白与换行
- token：含空格、中文、`#`、`%` 的路径往返编解码一致
- 搜索：忽略目录、深度/条目/结果上限、符号链接逃逸被排除、排序规则
- 引用解析：`..` / 绝对路径 / 工作区外 400；超过 5 个 400；重复去重
- 注入：≤8KB 全文、>8KB 摘要、图片 `data_url`、二进制仅路径、32KB 预算降级
- 变更：mtime/size 不一致 `changed=true`
- stat：不存在文件 `exists=false`；最多 50 条

### 集成

- `/api/chat` 带 references → 助手消息包含引用内容 → 历史返回 `_references`
- 远程模式：上传 403、references 403、历史查看正常
- 自动化任务执行不携带 references
- 旧会话（无 `_references`）加载不报错

### 前端

- `npm run build`
- 手工冒烟：@ 面板键盘操作、最近使用、上传多选/拖拽/粘贴、chip 整块删除、编辑重发还原引用、变更/删除标记

## 14. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-10 | 初版：确认混合注入、服务端搜索、多选上传、快照引用、远程只读限制 |
