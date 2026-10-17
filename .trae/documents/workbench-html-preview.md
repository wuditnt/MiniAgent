# 工作台 HTML / MHTML 预览实施计划

## 1. Summary

给工作台文件标签页增加 HTML 与 MHTML 预览：

- 支持 `.html` / `.htm`
- 支持单文件归档 `.mht` / `.mhtml`
- 默认进入 **预览**，工具栏提供「预览 / 源码」切换
- 预览在 **sandbox iframe** 中渲染，按已确认的选择**允许脚本执行**
- HTML 支持本地相对资源（CSS / 图片 / JS）
- MHTML 由后端解析归档，提取主 HTML 与内嵌资源后在同一个 iframe 中预览
- 源码模式复用现有代码高亮（Prism `markup`）

## 2. Current State Analysis

- [`FilePreview.tsx#L64-L172`](file:///d:/codeworks/mini-agent/web/src/components/FilePreview.tsx#L64-L172)：
  - 已有 Markdown 的预览/源码切换；
  - 文本统一走 `CodeBlock`，图片走 raw 接口；
  - 需要把 `mdView` 泛化为通用 `viewMode`，并接入 HTML / MHTML 预览。
- [`CodeBlock.tsx#L38-L50`](file:///d:/codeworks/mini-agent/web/src/components/CodeBlock.tsx#L38-L50)：
  - `EXT_LANG` 已包含 `html` / `htm` → `html`（Prism markup）。
- [`webui.py#L1600-L1725`](file:///d:/codeworks/mini-agent/src/core/webui.py#L1600-L1725)：
  - `/api/workspace/file` 把所有非图片文件当文本读取，`.mht` 会显示 MIME 头和 base64 乱码；
  - `/api/workspace/raw` 只服务图片，无法加载 CSS/JS；
  - 路径安全统一由 `_workspace_path()` 处理（越界 / `.memory`）。
- [`webui.py#L106-L141`](file:///d:/codeworks/mini-agent/src/core/webui.py#L106-L141)：
  - `_CONTENT_TYPES` 已覆盖 html/css/js/svg/png 等 MIME。
- [`test_workbench.py#L156-L165`](file:///d:/codeworks/mini-agent/tests/test_workbench.py#L156-L165)：
  - 已有「非图片 raw → 403」断言；raw 语义保持不变，新增接口不影响该用例。
- Python 标准库 `email` 可直接解析 MHTML，无需新增依赖。

## 3. 安全模型（已确认）

- iframe 使用 `sandbox="allow-scripts"`：
  - **不加** `allow-same-origin` → 页面处于 opaque origin，无法读取父页面 DOM、localStorage、cookie；
  - 允许脚本执行（用户已确认）；
  - 不开放表单提交、弹窗、下载等能力。
- 已知边界：允许脚本后，脚本仍可向外部地址发起网络请求。预览文件应视为用户可信内容。
- 本地资源只读：资源请求严格限制当前工作区，`../`、绝对路径、`.memory` 均拒绝。
- MHTML 内嵌资源一律转成 `data:` URL，不落盘、不执行额外命令。

## 4. Backend Changes

### 4.1 新增只读资源接口（`src/core/webui.py`）

新增：

```text
GET /api/workspace/asset/<workspace_id>/<相对路径>
```

为什么用路径式：

- HTML 预览注入 `<base href="/api/workspace/asset/<wid>/<文件目录>/">`；
- 相对 `style.css`、`images/a.png`、`app.js` 会自动拼成路径式 URL；
- CSS 内部的 `url(...)` 也能按目录解析（普通 HTML 场景）。
- 若用 `?path=` 查询参数，相对 URL 会丢掉 query，无法定位工作区。

行为：

- `unquote` 解码后交给现有 `_workspace_path(wid, rel)` 校验；
- 工作区内普通文件才允许；越界 / `.memory` 返回 403；
- 不存在 404；超过 20MB 返回 413；
- `Content-Type` 取 `_CONTENT_TYPES`，未知类型 `application/octet-stream`；
- 响应头：`Cache-Control: no-cache`、`X-Content-Type-Options: nosniff`；
- `/api/workspace/raw` 保持原样（仅图片）。

路由：

```python
m = re.match(r"^/api/workspace/asset/([^/]+)/(.+)$", route)
if m:
    self._handle_workspace_asset(m.group(1), unquote(m.group(2)))
```

需要把 `unquote` 加入 `urllib.parse` 导入。

### 4.2 新增 MHTML 解析模块（`src/core/mhtml.py`）

标准库实现，不引入依赖：

```python
def parse_mhtml(data: bytes, base_name: str = "") -> tuple[str, dict[str, str]]:
    """返回 (主 HTML, 资源映射)；资源值为 data: URL。"""
```

解析规则：

1. `email.parser.BytesParser(policy=policy.default).parsebytes(data)`；
2. `walk()` 所有 part，跳过 `multipart/*`；
3. 第一个 `text/html` part → 主文档：
   - `get_payload(decode=True)` 自动处理 base64 / quoted-printable；
   - 按 `get_content_charset()` 解码，失败退回 `sandbox.decode_bytes()`。
4. 其他 part → `data:<mime>;base64,<...>`，建立多组别名键：
   - `Content-ID`：同时登记 `cid:xxx` 与 `xxx`（去掉尖括号）；
   - `Content-Location`：登记完整 URL、URL path、basename；
   - 便于 HTML 中 `cid:`、相对路径、完整 URL 三种引用方式命中。
5. 找不到 `text/html` → 抛 `ValueError("归档里没有 HTML 主体")`；
6. 解析异常由 handler 转成 `kind:"unsupported"`，不返回 500。

### 4.3 扩展 `/api/workspace/file`

在现有文本读取之前增加 MHTML 分支：

- 扩展名（小写）∈ `{".mht", ".mhtml"}`：
  - 大小 > 20MB → `kind:"unsupported"`；
  - 读取完整字节 → `parse_mhtml()`；
  - 成功返回：

```json
{
  "kind": "mhtml",
  "name": "page.mht",
  "path": "docs/page.mht",
  "ext": ".mht",
  "size": 123456,
  "mtime": 1790000000.0,
  "html": "<html>…</html>",
  "resources": {
    "cid:img1@test": "data:image/png;base64,…",
    "pic.png": "data:image/png;base64,…"
  }
}
```

  - 失败返回 `kind:"unsupported"` + `reason`。

普通 `.html/.htm` 仍走文本分支（`kind:"text"`, `content`）。

### 4.4 测试（扩展 `tests/test_workbench.py`）

新增第 8、9 组用例。

HTML + 资源接口：

- 准备 `page.html`（引用 `style.css`、`pic.png`、`app.js`）；
- 断言 `/api/workspace/file?path=page.html` → `kind=text`、`ext=.html`；
- `/api/workspace/asset/default/style.css` → 200 + `text/css`；
- `/api/workspace/asset/default/pic.png` → 200 + `image/png`；
- `/api/workspace/asset/default/app.js` → 200 + `text/javascript`；
- 资源接口 `../`、`.memory` → 403；不存在 → 404；
- `/api/workspace/raw` 非图片仍 403（原语义）。

MHTML：

- 用 `email.message.EmailMessage` 构造 `multipart/related`：
  - HTML part：`<img src="cid:img1@test"><link rel="stylesheet" href="style.css">`
  - 图片 part：`Content-ID: <img1@test>`、`Content-Location: pic.png`、base64 PNG
  - CSS part：`Content-Location: style.css`
- 写成 `page.mht` 与 `page.mhtml`；
- 断言：
  - `/api/workspace/file?path=page.mht` → `kind=mhtml`；
  - `html` 包含原始 `<img>`；
  - `resources` 中存在 `cid:img1@test`、`pic.png`、`style.css`，值均以 `data:` 开头；
  - `.mhtml` 同样可解析；
  - 构造一个无 HTML 主体的 MHTML → `kind=unsupported`。

## 5. Frontend Changes

### 5.1 类型与 API

[`types.ts`](file:///d:/codeworks/mini-agent/web/src/types.ts)：

- `WorkspaceFileData.kind` 增加 `"mhtml"`；
- 新增可选字段：
  - `html?: string`
  - `resources?: Record<string, string>`

`api.ts` 不需要新函数：MHTML 由 `/api/workspace/file` 返回；普通 HTML 资源用现有路径拼 URL。

### 5.2 新增 `HtmlPreview` 组件

文件：`web/src/components/HtmlPreview.tsx`

```tsx
<HtmlPreview html={html} workspaceId={workspaceId} filePath={path} resources={resources} />
```

职责：

1. `DOMParser` 解析 HTML；
2. 确保 `<meta charset="utf-8">`；
3. 插入/替换 `<base href="/api/workspace/asset/{wid}/{fileDir}/">`；
4. 资源映射（仅 MHTML）：
   - 遍历 `[src]`、`[href]`、`[poster]`；
   - 属性值去掉 `./`、URL 解码后查 `resources`；
   - 命中则替换为 `data:` URL；
   - `<style>` 文本中按资源键做一次字符串替换（覆盖 `url(cid:...)`）。
5. 根相对路径（以 `/` 开头、非 `//`）改写为 `/api/workspace/asset/{wid}/...`；
6. 序列化后放入：

```tsx
<iframe
  title={...}
  sandbox="allow-scripts"
  srcDoc={doc}
  referrerPolicy="no-referrer"
  className="wb-html-frame"
/>
```

- 绝对 URL（http/https/data）、锚点、协议相对 `//` 保持不变；
- 脚本在 sandbox 内执行；
- 截断内容沿用现有提示。

### 5.3 `FilePreview` 集成

- `mdView` 重命名为 `viewMode: "preview" | "source"`；
- 类型判断：
  - `isMarkdown = [".md", ".markdown"]`
  - `isHtml = [".html", ".htm"]`
  - `isMhtml = [".mht", ".mhtml"]`
  - `canToggle = isMarkdown || isHtml || isMhtml`
- 打开新文件重置为 `preview`；
- 工具栏：
  - `canToggle` 时显示 `Segmented`（预览 / 源码）；
  - HTML / MHTML 预览时显示「脚本已启用（沙箱隔离）」提示；
- 内容渲染：
  - HTML + preview → `<HtmlPreview html={data.content} ... />`
  - MHTML + preview → `<HtmlPreview html={data.html} resources={data.resources} ... />`
  - Markdown + preview → 现有 `<Markdown />`
  - 其他 / source → `<CodeBlock lang={langFromExt(ext)} showLineNumbers />`
    - MHTML 的源码模式展示**提取后的主 HTML**（不是原始 MIME 归档）；
- 复制按钮：优先复制 `data.content`，MHTML 复制 `data.html`。

### 5.4 CSS（`web/src/global.css`）

```css
.wb-html {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.wb-html-frame {
  flex: 1;
  width: 100%;
  min-height: 0;
  border: 0;
  background: #fff;
}
```

- `wb-file` / Spin 容器的 flex 高度已修复，iframe 能正确撑满并内部滚动。

## 6. Assumptions & Decisions

- 允许脚本，但不给 same-origin；不提供禁用脚本开关。
- 资源接口只读、仅当前工作区；raw 接口继续只服务图片。
- MHTML 采用「后端解析 + 前端 data: 内联」方案，不新增虚拟目录路由。
- MHTML 的源码模式显示提取后的 HTML；不展示原始 MIME 归档。
- MHTML 内 CSS 的相对 `url()`：在 `<style>` 中按资源键替换；外链 CSS 转 data URL 后其内部相对路径可能无法解析，属于已知边界。
- 不引入第三方依赖（DOMPurify 等）；隔离交给 iframe sandbox。

## 7. Verification Checklist

1. `tests/run_tests.py` 全量通过（含 HTML / MHTML 新用例）。
2. `web` 目录 `npm run build` 通过。
3. 浏览器手工验收：
   - `.html` 默认预览，相对 CSS/图片/JS 正常加载，脚本执行；
   - `.mht` / `.mhtml` 默认预览，图片与样式正常；
   - 预览 / 源码切换正常；源码有行号与高亮；
   - 关闭标签、抽屉滚动、宽度拖拽、全屏不回归。
4. 安全验收：资源接口 `../`、绝对路径、`.memory` 被拒绝；iframe 无法访问父页面存储（sandbox 无 same-origin）。
