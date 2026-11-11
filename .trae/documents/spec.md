# @ 命令（文件引用与即时上传）技术规格

> 来源：[at-mention-plan.md](file:///d:/codeworks/mini-agent/.trae/documents/at-mention-plan.md)（方案已评审确认）
> 状态：待实施，需用户批准后进入编码
> 基线：`d38d1eb`（dev 分支，工作区干净）

## 1. 目标

在输入框中用 `@` 唤起文件面板，可引用当前工作区文件作为上下文，也可从本地选择图片/文件即时上传。引用随消息发送，历史可回看，文件后续变更/删除只做标注。

## 2. 不可偏离的决策

| # | 决策 |
|---|---|
| D1 | 混合注入：文本 ≤ 8KB 内联全文；> 8KB 给路径 + 前 40 行摘要；图片 ≤ 24MB 走多模态；其余仅路径 |
| D2 | 服务端递归搜索；跳过忽略目录；单条消息引用 ≤ 5 |
| D3 | 本地上传复用现有链路；多选、拖拽、粘贴；单文件 ≤ 24MB |
| D4 | `@` 仅在行首或空白后触发；`/` 仍只做技能；两个面板互斥 |
| D5 | 发送时记录快照元数据；后续变更只标注，不重读 |
| D6 | 远程只读：禁止上传与新增引用；历史引用可查看 |
| D7 | 自动化任务 P0 不传 references |
| D8 | 引用 ≤ 5、附件 ≤ 5；内联总预算 32KB，超出降级为路径引用 |
| D9 | 工作区引用不复制；本地文件复制进 `<workspace>/.attachments/` |

## 3. 后端改动

### 3.1 `src/core/webui.py`

新增常量（放在 `MAX_ATTACHMENT_TEXT` 一带）：

```python
MAX_REFERENCES = 5                  # 单条消息最多引用工作区文件数
REF_INLINE_BYTES = 8 * 1024         # 单文件内联上限
REF_TOTAL_INLINE_BYTES = 32 * 1024  # 单条消息内联总预算
REF_SUMMARY_LINES = 40              # 大文件摘要行数
REF_TEXT_EXTS = {...}               # md/txt/py/ts/tsx/js/jsx/json/yaml/yml/toml/ini/csv/log/sql/sh/html/css/xml 等
SEARCH_SKIP_DIRS = {...}            # .git node_modules .venv venv dist build out __pycache__ .mypy_cache .pytest_cache .idea .vscode .attachments .memory
SEARCH_MAX_ENTRIES = 20000
SEARCH_MAX_DEPTH = 12
SEARCH_MAX_RESULTS = 30
SEARCH_TIME_LIMIT = 1.5
STAT_MAX_PATHS = 50
```

新增模块级函数：

1. `_reference_file(ws_root, rel) -> str | None`
   - 拒绝绝对路径 / 盘符 / `..` 段；
   - `realpath` 后必须位于工作区根内；
   - 返回真实路径，非法返回 `None`。
2. `_resolve_references(references, ws_root) -> list[dict]`
   - 输入 `[{path, mtime, size}]`；去重（realpath）、超 5 个抛 `ValueError`；
   - 逐项 `stat`，与客户端 mtime/size 比对产出 `changed`；
   - 判定 `mode`：`inline`（文本且 ≤ 8KB，读全文）/ `image`（`image/*` 且 ≤ 24MB，读 `data_url`）/ `path`；
   - 文本 > 8KB：读前 40 行写入 `summary`，mode 仍为 `path`；
   - 总预算 32KB：按顺序累加，超出后降级 `path`；
   - 产出元数据：`{name, path, mime, size, mtime, mode, changed, content?, summary?, data_url?}`。
3. `_guess_mime(path)`：按 `_CONTENT_TYPES` 与 `mimetypes.guess_type` 兜底。
4. 搜索辅助：`_search_workspace(root, keyword, limit) -> (items, truncated)`，`os.walk` + 目录剪枝 + 条目/深度/结果/耗时上限，匹配排序：文件名前缀 > 文件名子串 > 路径子串 > 文件名字序列，同分按 `mtime` 倒序；路径统一正斜杠。

新增处理器：

5. `_handle_workspace_search(params)` → `GET /api/workspace/search?workspace_id=&q=&limit=`
   返回 `{items:[{name,path,size,mtime,ext}], truncated, took_ms}`。
6. `_handle_workspace_stat()` → `POST /api/workspace/stat`，体 `{workspace_id, paths}`，最多 50；返回 `{items:[{path,exists,size,mtime}]}`。

路由注册：

- `do_GET`：`elif route == "/api/workspace/search": self._handle_workspace_search(parse_qs(parsed.query))`
- `do_POST`：`elif route == "/api/workspace/stat": self._handle_workspace_stat()`

`_handle_upload`：远程只读时 403（`if self._is_remote(): self._fail(403, "远程访问不支持上传附件")`）。

`_handle_chat`：若请求体带非空 `references` 且 `self._is_remote()` → 403。

`start_run`：

- 解析 `references`：仅保留 dict、`path` 非空、最多 5 条，规范成 `[{path,mtime,size}]`；非法抛 `RunStartError(400, ...)`；
- `session.pending_turn` 增加 `"references": refs`；
- 工作线程参数追加 `refs`（自动化任务不传，保持 `[]`）。

`_run_worker`：签名增加 `references: list[dict] | None = None`；调用
`agent.run(message, attachments=_resolve_attachments(...), references=_resolve_references(references or [], ws_root), extra_system=skill_context)`。

历史链路：

- `_Session` 恢复磁盘消息时：`if role == "user" and m.get("references"): msg["_references"] = m["references"]`
- `save()`：`if role == "user" and m.get("_references"): item["references"] = m["_references"]`
- `_with_pending_user`：`if pending.get("references"): item["_references"] = list(...)`
- `_display_items`：用户项 `refs = raw[i].get("_references") or raw[i].get("references")` → `item["references"] = refs`

### 3.2 `src/core/agent.py`

- `_build_user_message(user_input, attachments, references=None)`：
  - `inline`：追加 `\n\n[引用文件 <path> 内容]\n<content>`
  - `image`：`data_url` 作为 `image_url` 分片
  - `path`：追加 `\n\n[引用工作区文件 <path>（N 字节，未内联，可用文件工具读取）]`，有 `summary` 时附摘要
  - 元数据 `_references`（去掉 `content`/`data_url` 等大字段，只留展示字段）
- `run(..., references=None)` 透传；`_run_loop(user_input, attachments, references)`；`USER_MESSAGE` 事件带 `references` 元数据。
- `run.start` 日志/事件增加引用数量。

## 4. 前端改动

| 文件 | 改动 |
|---|---|
| `web/src/types.ts` | `WorkspaceRef`、`WorkspaceSearchItem`、`ChatItem.references`、`HistoryMessage.references`；`onSend` 增加 references 参数 |
| `web/src/api.ts` | `searchWorkspaceFiles(workspaceId, q, limit)`、`statWorkspaceFiles(workspaceId, paths)`；`uploadFile` 保持不变 |
| `web/src/components/composer/mention.ts`（新增） | `MFILE` 标记 `\u0003file:<encodeURIComponent(path)>\u0003`；`detectMention(prev,next)`、`mentionQuery(value,index)`、`stripMentionCommand(value,index)`、`fileToken`、`filesInValue`、`insertFileToken`、`stripFileMarks`、`replaceFileTokensWithPaths` |
| `web/src/components/composer/MentionMenu.tsx`（新增） | 分组面板：工作区文件 / 最近使用 / 从本地上传；150ms 防抖搜索；↑↓ Enter/Tab Esc；最多 20 条 |
| `web/src/components/composer/SkillInput.tsx` | token 正则扩展为技能 + 文件两类；文件 chip 渲染（图标 + 文件名，`title` 为相对路径）；序列化回标记 |
| `web/src/components/chat/FileRefChips.tsx`（新增，或放 `components/`） | 引用 chip；挂载时批量 `statWorkspaceFiles` 标注「已变更/已删除」；点击走现有预览 |
| `web/src/components/Chat.tsx` | `selectedRefs` 状态；mention 面板状态与 `/` 面板互斥；`handleInputChange` 接入 `detectMention`；选择后插 token；发送时剥离 token 并透传 references；多选上传（`multiple`）、拖拽、粘贴；编辑重发还原 token；引用上限 5 |
| `web/src/App.tsx` | `send` 增加 references 参数；乐观气泡带 `references`；请求体透传 |
| `web/src/global.css` | mention 面板分组、文件 chip、变更/删除标记样式 |

## 5. 验收标准

### 后端

- `tests/test_mention.py` 覆盖：搜索命中并跳过忽略目录、深度/结果截断、非法路径 400、文本内联、大文件降级、图片 `data_url`、32KB 预算降级、`changed` 标记、stat 不存在文件、超 5 个 400、远程 403、历史回放带 `references`。
- `.\\.venv\\Scripts\\python.exe -m pytest tests -q` 全绿（含既有 25 + 14 项）。

### 前端

- `cd web && npm run build`（`tsc --noEmit && vite build`）通过。

### 手工冒烟

- `@` 搜索选文件 → chip → 发送 → 气泡显示引用 → 点击预览；
- 本地上传图片（多选/拖拽/粘贴）→ 发送 → 多模态可见；
- 文件发送后修改/删除 → 历史 chip 标注；
- `/` 技能命令、附件上传、SSE 无回归。

## 6. 文档

- 更新 `docs/03-features/chat-and-agent.md` 输入框能力一节；
- 必要时在 `docs/README.md` 补索引。

## 7. 需要用户批准

批准本规格后即可按 [tasks.md](file:///d:/codeworks/mini-agent/.trae/documents/tasks.md) 顺序执行，中途不再询问。
