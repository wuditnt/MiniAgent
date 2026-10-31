# 工作区与文件抽屉

## 用途

右侧抽屉用于**只读**浏览当前工作区的文件，并提供多格式预览。它与「工作区管理」（左侧按工作区分组会话）不是一回事：

- **工作区**：一个目录，就是 Agent 的文件操作边界（沙箱根）。左侧边栏可新建 / 改名 / 移除；默认工作区锚在用户数据目录下的 `~/mini-agent`，不可删除。
- **文件抽屉**：浏览当前工作区的文件，不提供编辑保存。

## 使用方式

- 入口：顶栏最右侧布局图标。桌面端默认 420px，可拖拽左边缘调宽并记住；右上角「全屏」占满主区（保留左侧导航），再次点击还原。窄屏（≤1000px）为右侧浮层。
- 顶部标签页：`文件` 列表页 + 每个已打开文件一个标签；同一文件不重复开。
- 文件列表：单层目录 + 面包屑，目录在前、文件在后；隐藏 `.memory` 与 `.attachments`。
- 文件行 hover 出现下载按钮（触屏常显）。
- 关闭全部标签 → 抽屉自动收起；再次打开回到「开始」页。

### 预览格式

| 格式 | 预览 |
|---|---|
| 代码 / 纯文本 | Prism 高亮 + 行号 |
| Markdown | 预览 / 源码切换 |
| HTML / MHTML | sandbox iframe 预览，可切源码 |
| 图片（png / jpg / gif / webp / bmp / svg） | 直接渲染 |
| PDF | pdf.js：翻页 / 缩放 / 适应宽度 |
| CSV / TSV | 表格，行/列分页 |
| 其他二进制 / 超大文件 | 占位说明 |

文本最多展示 20MB（另限 5000 行、单行 4000 字符）；PDF 等原始字节预览上限 20MB。

HTML / MHTML：

- `.html` / `.htm` 在 `sandbox="allow-scripts"` 的 iframe 中渲染，脚本可运行但没有同源权限；相对 CSS / 图片 / JS 经 `/api/workspace/asset` 加载。
- `.mht` / `.mhtml` 由后端标准库 `email` 解析，提取主 HTML 与内嵌资源，以 `data:` URL 注入。

CSV / TSV：

- 后端用标准库 `csv` 流式解析（自动处理引号、字段内换行；`.csv` 嗅探分隔符，`.tsv` 固定 Tab），按「行窗口 × 列窗口」返回，单页 200 行 × 50 列；解析上限 20MB / 10000 行 / 1000 列。
- 前端冻结表头与序号列，行/列各自翻页；首行作表头，空列名补 `列N`，重名列加后缀。

## 扩展一种预览格式

只需两处：

1. 后端 `_BINARY_PREVIEWS`（扩展名 → kind）；
2. 前端 `web/src/components/preview/registry.tsx`（kind → 懒加载组件）。

## 接口

| 接口 | 用途 |
|---|---|
| `GET /api/workspace/dir` | 目录列表 |
| `GET /api/workspace/file` | 文本 / 结构化预览 |
| `GET /api/workspace/raw` | 原始字节（图片 / PDF 等） |
| `GET /api/workspace/download` | 下载原文件 |
| `GET /api/workspace/table` | CSV / TSV 行窗口 |
| `GET /api/workspace/asset` | HTML 预览的相对资源 |

## 关键代码位置

- 工作区配置：`src/core/workspace_config.py` 的 `ensure_default_workspace()` / `list_workspaces()` / `create_workspace()` / `update_workspace()` / `delete_workspace()` / `resolve_path()`
- 文件接口：`src/core/webui.py` 的 `_handle_workspace_dir()` / `_handle_workspace_file()` / `_handle_workspace_raw()` / `_handle_workspace_download()` / `_handle_workspace_table()` / `_handle_workspace_asset()` / `_read_table()`
- 预览类型表：`_BINARY_PREVIEWS`
- MHTML：`src/core/mhtml.py`
- 前端：`web/src/components/Workbench.tsx`、`WorkbenchFiles.tsx`、`FilePreview.tsx`、`HtmlPreview.tsx`、`components/preview/registry.tsx`、`PdfPreview.tsx`、`TablePreview.tsx`
- 测试：`tests/test_workbench.py`

## 注意事项

- 接口严格限制在当前工作区内：`../`、绝对路径与 `.memory` 均被拒绝。
- 文件抽屉是只读的，改文件请让 Agent 用工具，或在编辑器里改。
- 工作区有会话时不允许删除。
- 已开始对话的工作区只读显示，不能切换沙箱根 —— 否则「之前写的文件在哪」难以理解。
