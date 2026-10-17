# 右侧工作区抽屉（文件浏览）实施计划

## 1. Summary

在主界面右侧增加一个可展开/收起的**工作台抽屉**，用于浏览当前工作区文件：

- 顶栏最右侧新增抽屉开关图标（图 1 箭头位置）。
- 抽屉顶部是标签页，底部是内容区；初始显示「开始」快捷键页（图 2）。
- 点击「工作区文件」进入文件列表页（图 3）：单层目录列表 + 面包屑。
- 点击文件新建一个文件标签页（图 4 / 图 5），支持代码/文本、Markdown、图片预览。
- 关闭全部标签页后抽屉自动收起；再次打开回到「开始」页。

本轮为**只读浏览**，不提供编辑保存。

## 2. Current State Analysis

前端：

- [`App.tsx#L584-L610`](file:///d:/codeworks/mini-agent/web/src/App.tsx#L584-L610)：整体是 `sidebar-fixed + main` 的横向 flex 布局，抽屉可直接作为第三个 flex 子项。
- [`App.tsx#L649-L706`](file:///d:/codeworks/mini-agent/web/src/App.tsx#L649-L706)：顶栏右侧依次是统计、清空、导出、模型菜单、联网开关，抽屉开关图标放在这一排末尾。
- [`CodeBlock.tsx#L22-L57`](file:///d:/codeworks/mini-agent/web/src/components/CodeBlock.tsx#L22-L57)：已有 PrismLight + oneLight + 11 种语言注册，但 `EXT_LANG` 未导出，且没有行号能力。
- [`Markdown.tsx#L58-L60`](file:///d:/codeworks/mini-agent/web/src/components/Markdown.tsx#L58-L60)：已有 GFM / KaTeX / 清洗后的 HTML 渲染，可复用于 `.md` 预览。
- [`types.ts#L79-L125`](file:///d:/codeworks/mini-agent/web/src/types.ts#L79-L125)：已有 `AgentState` / `WorkspaceItem`；需要新增工作台文件类型。
- [`api.ts`](file:///d:/codeworks/mini-agent/web/src/api.ts)：统一封装 fetch；需要新增目录/文件接口。

后端：

- [`webui.py#L832-L851`](file:///d:/codeworks/mini-agent/src/core/webui.py#L832-L851)：GET 路由集中分发，新增接口挂在这里。
- [`webui.py#L1454-L1488`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1454-L1488)：已有 `_handle_attachment_serve` 的二进制流式输出与路径校验模式，可复用思路。
- [`webui.py#L106-L124`](file:///d:/codeworks/mini-agent/src/core/webui.py#L106-L124)：已有 `_CONTENT_TYPES` MIME 表，需补充图片/文本类型。
- [`workspace_config.py`](file:///d:/codeworks/mini-agent/src/core/workspace_config.py)：`resolve_path(workspace_id)` 可从工作区 id 得到真实根目录。
- [`sandbox.py#L197-L209`](file:///d:/codeworks/mini-agent/src/core/sandbox.py#L197-L209)：已有 `looks_binary()` 与 `decode_bytes()`（UTF-8 → GBK → Latin-1），可直接复用。
- 目前**没有**工作区目录/文件读取 API，需要新增。

## 3. UX 设计

### 3.1 整体布局

```text
┌─────────┬──────────────────────────────┬──────────────────────┐
│ 侧边栏   │ 主区（顶栏 + 对话 + 输入区）      │ 工作台抽屉            │
│         │                              │ ┌ 标签栏 ───────────┐ │
│         │                              │ │ 文件 × │ a.py × │ + │
│         │                              │ ├ 内容区 ───────────┤ │
│         │                              │ │ 文件列表 / 文件内容 │ │
│         │                              │ └───────────────────┘ │
└─────────┴──────────────────────────────┴──────────────────────┘
```

- 桌面端：抽屉参与 flex 布局，宽 **420px**，展开时主区自动缩窄；收起时宽 0。
- 窄屏（≤1000px）：抽屉改为右侧浮层（`position: absolute`，宽 `min(420px, 92vw)`，带阴影），不挤压对话区。
- 抽屉高度撑满视口，内部独立滚动，不影响聊天页滚动。

### 3.2 抽屉状态机

标签页只存在于内存，不持久化：

| 状态 | 行为 |
| --- | --- |
| 没有任何标签 | 显示「开始」页（图 2） |
| 打开「工作区文件」 | 新建/激活 `文件` 标签（`id="files"`） |
| 点击文件 | 新建 `file:<path>` 标签；同一文件已打开则直接激活 |
| 关闭某个标签 | 激活相邻标签；若关闭后没有任何标签 → 清空状态并 `onClose()` 收起抽屉 |
| 手动收起抽屉 | 保留当前标签；再次展开继续原标签 |
| 全部标签关闭后再次展开 | 标签已被清空 → 显示「开始」页 |
| 切换工作区 | 清空标签与目录状态，回到「开始」页（抽屉保持当前开合状态） |

### 3.3 开始页（图 2）

- 顶部标题「开始」。
- 三个快捷键卡片：
  - **工作区文件**：可点击，进入文件列表页。
  - **新建终端**、**浏览器**：展示为禁用状态（`即将支持`），不实现功能，仅保持参考图布局。
- 卡片样式：浅色底、左侧图标、标题 + 说明。

### 3.4 文件列表页（图 3）

- 标签：`文件` + 关闭按钮。
- 顶部一行：工作区根路径面包屑（可点击回上级/根）。
- 工具栏：刷新按钮。
- 列表：目录在前、文件在后，按名称排序；每项显示类型图标、名称、大小、修改时间。
- 点击目录：进入该目录，面包屑追加。
- 点击文件：打开文件标签。
- 隐藏内部目录：`.memory`（记忆保护区）与 `.attachments`（上传附件缓存）。
- 目录条目超过 500 条时返回 `truncated: true`，页面提示「仅显示前 500 项」。

### 3.5 文件标签页（图 4 / 图 5）

标签栏：

- 每个标签显示文件类型图标 + 文件名 + 关闭按钮。
- 标签过多时横向滚动，不换行。
- 最右侧固定 `+` 按钮：重新打开/激活「文件」标签。

内容区顶部工具栏：

- 面包屑：工作区名 / 目录 / 文件名。
- 右侧：只读标识、文件大小、刷新按钮。
- Markdown 文件额外提供「预览 / 源码」切换；默认预览。

内容渲染：

| 类型 | 渲染方式 |
| --- | --- |
| 代码 / 纯文本 | Prism 语法高亮 + 行号 + 横向滚动（不折行），复用 `CodeBlock` 扩展 |
| Markdown | 复用 `Markdown` 组件渲染；工具栏可切源码 |
| 图片（png/jpg/jpeg/gif/webp/bmp/svg） | `<img>` 从 raw 接口加载，居中显示，最大宽高 100% |
| 其他二进制 | 显示「暂不支持预览」+ 文件大小 + 扩展名，不展示乱码 |
| 超大文本 | 最多读取 1MB / 5000 行，底部提示「内容已截断」 |

## 4. Backend Changes

### 4.1 新增三个只读接口（`src/core/webui.py`）

1. `GET /api/workspace/dir?workspace_id=<id>&path=<相对路径>`

```json
{
  "workspace_id": "default",
  "root": "C:\\Users\\me\\mini-agent",
  "path": "sub/dir",
  "parent": "sub",
  "entries": [
    {"name": "src", "path": "sub/dir/src", "type": "dir", "size": 0, "mtime": 1790000000.0, "ext": ""},
    {"name": "a.py", "path": "sub/dir/a.py", "type": "file", "size": 1234, "mtime": 1790000000.0, "ext": ".py"}
  ],
  "truncated": false
}
```

2. `GET /api/workspace/file?workspace_id=<id>&path=<相对路径>`

```json
{
  "kind": "text",
  "name": "a.py",
  "path": "sub/dir/a.py",
  "ext": ".py",
  "size": 1234,
  "mtime": 1790000000.0,
  "encoding": "utf-8",
  "content": "…",
  "lines": 65,
  "truncated": false
}
```

- `kind` 取值：`text` / `image` / `binary` / `unsupported`。
- 图片返回 `kind:"image"` 与 `url`（raw 接口地址），不返回二进制正文。
- 二进制返回 `kind:"binary"`、`reason`、`size`。

3. `GET /api/workspace/raw?workspace_id=<id>&path=<相对路径>`

- 仅用于图片流式输出：扩展名白名单 + `_CONTENT_TYPES` 的 `image/*`。
- 非图片返回 403；单文件上限 20MB；`Cache-Control: no-cache`。

### 4.2 路径安全（所有新接口共用）

新增私有 helper `_workspace_path(workspace_id, rel) -> (root, abs_path)`：

- `root = os.path.realpath(workspace_config.resolve_path(workspace_id))`。
- 拒绝绝对路径（`os.path.isabs`）与包含 `..` 的越界结果。
- `ap = os.path.realpath(os.path.join(root, rel))`，必须满足 `ap == root` 或 `ap.startswith(root + os.sep)`。
- 拒绝 `.memory` 保护区（复用 `core.memory.is_memory_path(ap, root)`）。
- 目录遍历、文件读取都严格限制在当前工作区内，**不受** `sandbox_read` 的 `open/whitelist` 放开影响。
- 符号链接经 `realpath` 解析后仍必须落在工作区内。

### 4.3 文件分类与读取

- 目录列举：`os.scandir`，统计 `is_dir/is_file`、`stat().st_size/st_mtime`；排序规则目录优先、名称不区分大小写。
- 文件读取：
  - `stat` 后大于 20MB → `kind:"unsupported"`，提示文件过大。
  - 读取前 1MB 字节；`looks_binary()` 判定二进制。
  - 文本用 `decode_bytes()`，按 `\n` 切行；超过 5000 行截断并置 `truncated`。
  - 图片按扩展名判定，返回 raw URL。
- MIME 表补充 `.md` / `.csv` / `.log` / `.yaml` / `.yml` / `.toml` 等文本类型，图片沿用现有条目。

### 4.4 测试（新增 `tests/test_workbench.py`，加入 `tests/run_tests.py`）

不依赖模型，启动临时工作区 + WebServer：

1. 根目录列表：目录在前、文件按名排序、大小/时间字段存在。
2. 子目录导航：`path=sub` 返回正确 `parent`，可回到根。
3. 隐藏项：`.memory` / `.attachments` 不出现在列表。
4. 越界：`../`、绝对路径、指向工作区外的符号链接均返回 403/400。
5. 文本读取：UTF-8 与 GBK 文件都能正确解码，行数正确。
6. Markdown：返回 `kind:"text"`，扩展名 `.md`。
7. 图片：返回 `kind:"image"`，raw 接口 200 且 `Content-Type` 正确；伪造扩展名的非图片不返回图片 MIME。
8. 二进制：含 NUL 字节的文件返回 `kind:"binary"`。
9. 超大文件：超过行数/字节上限时 `truncated: true`。
10. 不存在的工作区/文件：404。

## 5. Frontend Changes

### 5.1 新增组件

- `web/src/components/Workbench.tsx`
  - 抽屉外壳：`open`、`workspaceId`、`onClose`。
  - 标签状态、开始页、标签栏、关闭/激活逻辑、全部关闭自动收起。
  - 切换 `workspaceId` 时重置标签与目录。
- `web/src/components/WorkbenchFiles.tsx`
  - 文件列表标签页：面包屑、刷新、目录/文件条目。
  - 负责调用 `fetchWorkspaceDir`，点击文件回调给 `Workbench`。
- `web/src/components/FilePreview.tsx`
  - 接收文件元数据，按 `kind` 分发到代码/文本、Markdown、图片、二进制占位。
  - 文本用扩展后的 `CodeBlock`；Markdown 用 `Markdown`；图片用 raw URL。

### 5.2 复用与扩展

- `CodeBlock.tsx`：
  - 导出 `langFromExt(ext)`（把现有 `EXT_LANG` 暴露为函数）。
  - 新增可选 `showLineNumbers?: boolean`；文件预览传 `true`。
  - 代码区保持 `whiteSpace: "pre"` + `overflowX: auto`，行号样式用 Prism 自带 `showLineNumbers` 配 `lineNumberStyle`。
- `Markdown.tsx`：
  - 新增可选 `assetBase?: { workspaceId: string; dir: string }`。
  - 通过 `urlTransform` 把 Markdown 中的相对图片路径映射到 `/api/workspace/raw?...`；绝对 http(s)/data URL 保持不变。
- `App.tsx`：
  - 新增 `workbenchOpen` 状态。
  - 顶栏最右侧加开关图标（`LayoutOutlined` 或 `BorderRightOutlined`），Tooltip「工作区」。
  - 在 `.main` 之后渲染 `<Workbench open={workbenchOpen} workspaceId={ws} onClose={() => setWorkbenchOpen(false)} />`。
- `api.ts` / `types.ts`：
  - `fetchWorkspaceDir(workspaceId, path)`、`fetchWorkspaceFile(workspaceId, path)`、`workspaceRawUrl(workspaceId, path)`。
  - 新增 `WorkspaceEntry`、`WorkspaceDirData`、`WorkspaceFileData` 类型。

### 5.3 CSS（`global.css`）

新增区块 `.workbench`：

- `.workbench`：`flex: none; width: 0; overflow: hidden; transition: width .2s; border-left: 1px solid #f0f0f0;`
- `.workbench.open`：`width: 420px;`
- `.workbench-inner`：固定 `width: 420px; height: 100%; display:flex; flex-direction:column;`
- 标签栏：40px 高、横向滚动、隐藏滚动条；活动标签浅蓝底 + 下划线。
- 内容区：`flex:1; overflow:auto;`
- 开始页卡片、文件列表行、文件工具栏、图片预览、二进制占位分别加 `.wb-*` 类。
- ≤1000px：抽屉改 `position:absolute; right:0; top:0; bottom:0; z-index:20; box-shadow;`，主区不再缩窄。

## 6. Assumptions & Decisions

- 抽屉宽固定 **420px**，不做拖拽调宽（后续可加）。
- 本轮**只读**：无编辑、无保存、无新建/删除/重命名。
- 格式范围：文本/代码、Markdown、图片；JSON/CSV 按文本展示（不额外做表格视图）；PDF / Office / 压缩包显示“暂不支持预览”。
- 目录浏览：单层列表 + 面包屑，不做树形。
- 文件列表隐藏 `.memory` 与 `.attachments`；其他点文件正常显示。
- 标签页状态仅内存保存；手动收起抽屉保留标签，全部标签关闭才清空并自动收起。
- 新增接口严格限制在工作区内，不受沙箱读取策略的 `open/whitelist` 影响。
- 大文件预览上限：文本 1MB / 5000 行，图片 20MB；超过提示截断或不可预览。
- 「新建终端」「浏览器」卡片仅作占位，不实现功能。

## 7. Verification

1. 后端：`.venv\Scripts\python.exe tests\run_tests.py`，新增套件 + 全量回归全绿。
2. 前端：`web` 目录 `npm.cmd run build`（含 tsc）。
3. 浏览器手工验收：
   - 顶栏图标可展开/收起抽屉；桌面端主区缩窄，窄屏浮层显示。
   - 开始页显示三张卡片，仅「工作区文件」可点击。
   - 文件页目录导航、面包屑返回、刷新正常。
   - 点击同一文件不会重复开标签；标签可关闭并激活相邻标签。
   - 关闭全部标签 → 抽屉自动收起；再次展开显示开始页。
   - `.py` 有行号与语法高亮；`.md` 渲染且可切源码；`.txt` 纯文本；图片正常显示；二进制显示占位。
   - 大文件显示截断提示。
   - 切换工作区后标签清空并回到开始页。
4. 安全验收：`../`、绝对路径、`.memory`、工作区外文件均被拒绝。
5. 回归：聊天页不出现横向滚动；现有设置/记忆/会话功能不受影响。
