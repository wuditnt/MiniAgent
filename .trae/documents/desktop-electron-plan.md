# 双形态方案：浏览器端 + Electron 桌面端（含局域网访问）

> 状态：方案待评审，**尚未实现**。
> 目标：一套代码同时支撑「浏览器访问」与「Electron 桌面应用」，并保留手机 / 局域网访问能力；
> 桌面版内置文档（docs / docs-fast）与 OCR 依赖，开箱即用。

## 0. 已确认的决策

| # | 决策 | 结论 | 影响 |
|---|---|---|---|
| D1 | OCR / 文档依赖 | **整体打包**，不拆包、不按需下载 | 单安装包，体积约 200–300 MB |
| D2 | 局域网访问 | 浏览器模式默认开放；**桌面版提供开关，默认关闭，重启后生效** | Electron 按开关把 `--host` 传给后端；开启后展示局域网地址与二维码 |
| D3 | 界面偏好存储 | **继续用 localStorage**，不上移后端 | 依赖固定端口保证 origin 不变 |
| D4 | 桌面端口 | **固定 `8317`，不允许用户修改** | 端口被占用时启动失败，明确提示用户排查 |

> D3 与 D4 是一组：偏好留在 localStorage，就必须保证 origin 恒定，所以端口不能漂移。
> 也因此**不做「端口被占用时回退随机端口」**——静默漂移会让用户悄悄丢掉当前会话与全部偏好。

---

## 1. 目标与边界

### 1.1 要达成的三件事

| 形态 | 启动方式 | 前端来源 | 数据目录 | 默认网络 |
|---|---|---|---|---|
| 浏览器（开发） | `startup_dev.bat` | Vite dev server `:5173` | 仓库内 `web_data/` | `0.0.0.0`，可被局域网访问 |
| 浏览器（生产） | `startup.bat` | 后端托管 `web/dist` | 仓库内 `web_data/` | `0.0.0.0` |
| 桌面应用 | 双击 exe | 后端托管内置 `web/dist`（同源） | `%APPDATA%/mini-agent/` | `8317`：默认 `127.0.0.1`，可开关局域网（重启生效） |

### 1.2 明确不做

- 不做 `file://` 加载前端（会引入 CORS、`origin: null`、相对路径全部失效等一堆问题）
- 不重写后端为 ASGI（现有 `ThreadingHTTPServer` 足够，见第 3 节说明）
- 不把 Python 逻辑搬到 Node（Python 是能力主体，Electron 只做壳）
- **不做运行中热切换监听地址**：局域网开关保存后需重启应用才生效（socket 绑定无法平滑迁移）

---

## 2. 总体架构

```
┌──────────────────── Electron 主进程（Node） ────────────────────┐
│ 1. 解析 userData 路径 / 读取 desktop-config.json（含 lan_enabled）         │
│ 2. spawn 内置后端：server.exe --desktop --host <127.0.0.1|0.0.0.0>        │
│                   --port 8317 --data-dir <userData>                       │
│ 3. 读 stdout 的 READY 行 → 拿到 {port, token, host}              │
│ 4. BrowserWindow.loadURL(http://127.0.0.1:<port>/?k=<token>)     │
│ 5. 退出：轮询 /api/shutdown 或发 SIGTERM → 超时 taskkill /T /F   │
└─────────────────────────────────────────────────────────────────┘
                              │ HTTP（同源）
┌─────────────────────────── 渲染进程 ────────────────────────────┐
│ 现有 React 应用（web/src）—— 少量桌面适配，不引入 IPC 依赖        │
└─────────────────────────────────────────────────────────────────┘
```

**核心决策：Electron 加载后端 URL，而不是加载本地 HTML 文件。**

- 前端所有请求都是相对路径（`web/src/api.ts` 里全部是 `/api/...`），同源加载即零改造
- SSE（`fetch` 流式）与 Cookie 鉴权都能天然工作
- 手机与桌面访问的是同一套页面，行为一致，减少双份维护

---

## 3. 为什么后端仍然用 `http.server`

现有点评（已核对代码）：

- `ThreadingHTTPServer` + `daemon_threads`（`webui.py:1037-1060`），一连接一线程
- `BaseHTTPRequestHandler` 子类，`protocol_version = "HTTP/1.1"`，keep-alive
- 手写 SSE 帧（`webui.py:1152-1166`），断线检测已实现
- 零第三方依赖（`pyproject.toml` 只有 `openai`）

Agent 主循环是同步的（同步 OpenAI SDK、阻塞式子进程、工具线程池），换 FastAPI 需要
`run_in_threadpool` 包一层，收益仅限于路由表和 OpenAPI，代价是 +fastapi/uvicorn/pydantic
三个依赖与 2200 行代码的回归面。**本次不迁移**，仅在桌面化中补齐下面 4 个缺口。

---

## 4. 后端改造清单

按「桌面化必需 → 体验优化」排序。每项都给出落点文件。

### B1. 固定端口与启动握手（必需）

- 桌面模式**端口固定 `8317`**，不提供修改入口（决策 D4）；监听地址由局域网开关决定（见 B7）
- 绑定失败（`OSError: WinError 10048`）时：
  - stdout 输出一行结构化错误，供 Electron 原样展示：
    `MINI_AGENT_ERROR {"code":"PORT_IN_USE","port":8317,"hint":"端口 8317 已被其他程序占用"}`
  - 进程以非 0 退出码结束，不做任何自动换端口的重试
- 绑定成功后输出就绪行：
  `MINI_AGENT_READY {"host":"127.0.0.1","port":8317,"pid":12345,"version":"1.0.0","token":"..."}`
- 现有打印用的是 `self.port`（`webui.py:1048-1057`），改从 `httpd.server_address[1]` 取，
  保证输出的是**实际绑定**的端口
- 浏览器模式仍保持 `--port 8000` 默认，行为不变

**落点**：`src/core/webui.py`（`serve_forever`）、`src/core/main.py`（`--port`）

### B2. 数据目录分离（必需）

打包后安装目录不可写，而目前所有可写数据都挂在 `ROOT` 下：

| 数据 | 当前位置 | 桌面版应迁往 |
|---|---|---|
| 配置 | `web_data/settings.json`（`settings.py:21`） | `<data>/web_data/settings.json` |
| 模型 Key | `web_data/models.json`（`model_config.py:23`） | `<data>/web_data/models.json` |
| 工作区表 | `web_data/workspaces.json`（`workspace_config.py:25`） | `<data>/web_data/workspaces.json` |
| 会话 | `web_data/sessions/`（`webui.py:56`、`events.py:42`） | `<data>/web_data/sessions/` |
| 用户画像 | `web_data/memory/`（`memory.py:115`） | `<data>/web_data/memory/` |
| 日志 | `logs/`（`logging_setup.py:27`） | `<data>/logs/` |

- 新增 `MINI_AGENT_DATA_DIR` 环境变量 / `--data-dir` 参数，在 `_paths.py` 里统一算出 `DATA_DIR`
- `ROOT` 继续表示「只读资源根」：插件、技能、`web/dist`、`mcp/`
- 桌面版默认 `data-dir = %APPDATA%/mini-agent`，浏览器版默认仍为 `ROOT`（行为不变）
- 用户插件 / 技能：打包后 `plugins_user/`、`skills_user/` 应落在数据目录而不是安装目录，
  通过 `AGENT_PLUGIN_DIRS` / `AGENT_SKILL_DIRS` 传**绝对路径**即可（loader 与 skills 的
  `os.path.join(root, d)` 对绝对路径会直接采用，无需改代码）

**落点**：`src/core/_paths.py`、`settings.py`、`model_config.py`、`workspace_config.py`、
`events.py`、`memory.py`、`logging_setup.py`、`webui.py`、`main.py`

### B3. 静态资源路径可配（必需）

`_DIST = os.path.join(ROOT, "web", "dist")`（`webui.py:55`）在打包布局里不再成立。

- 改为 `MINI_AGENT_WEB_DIST` 优先，缺省回落 `ROOT/web/dist`
- 打包时 `web/dist` 作为 extraResources 随安装包分发

**落点**：`src/core/webui.py:55`

### B4. 本地 API 鉴权（必需，尤其开局域网后）

现状：`/api/*` 完全无鉴权，而它能执行 shell、读写任意沙箱内文件、读配置。桌面上任何本机
进程、或浏览器里的恶意页面（配合 DNS rebinding）都可能直接调用。

设计（对前端零改动）：

1. 每次启动生成随机 token（32 字节 hex）
2. 首次访问 `GET /?k=<token>` → 校验后写 `Set-Cookie: ma_token=<token>; HttpOnly; SameSite=Lax; Path=/`
   并 302 到不带 `?k` 的地址（避免 token 留在地址栏 / 历史记录）
3. 所有 `/api/*` 校验 Cookie；`fetch` 同源会自动带上，无需改 `api.ts`
4. 静态资源不校验（内容本身不敏感，且要保证首屏能加载）
5. 校验 `Host` 头只允许 `127.0.0.1` / `localhost` / 本机局域网 IP / 用户配置的域名，
   防 DNS rebinding
6. 桌面版把带 token 的 URL 直接交给 `loadURL`；浏览器版启动时打印同样的 URL
7. 局域网配对体验：设置页展示当前局域网地址 + 二维码（内含 `?k=`），手机扫码即进

测试影响：`tests/test_webui.py`、`tests/test_http_e2e.py` 直接请求 `/api/*`，需要提供
`MINI_AGENT_AUTH=off`（仅测试/`--no-auth` 时生效）或让测试读取 READY 行里的 token。
**建议**：新增 `--no-auth` 开关，仅供本机调试与测试使用，README 注明风险。

**落点**：`src/core/webui.py`（请求入口统一校验）、`src/core/main.py`、`tests/*`

### B5. 健康检查（必需）

新增 `GET /api/health` → `{ready, version, pid, port, mode, lan_enabled, uptime}`。

用途：Electron 启动探活、崩溃自动重启判定、设置页展示状态。不复用 `/api/state`
（它包含会话信息，语义不匹配且更重）。

### B6. 优雅退出（必需）

当前只 catch `KeyboardInterrupt`（`webui.py:1059-1064`）。桌面版关闭窗口时进程收到的
是 `SIGTERM`（Windows 上 spawn 的进程需 `kill()` 或 `taskkill`），子进程与 MCP server
可能残留成孤儿。

- 注册 `SIGTERM` / `SIGBREAK` / `SIGINT`（Windows 用 `signal.signal` 支持的部分）
- 顺序：停止接受新请求 → 关闭 SSE 长连接 → `httpd.shutdown()` → `mcp_manager.shutdown()`
  → 取消正在运行的任务（`token.cancel()`）→ 退出
- 额外提供 `POST /api/shutdown`（需鉴权 + 仅本机），供 Electron 主动调用，比发信号跨平台更稳

**落点**：`src/core/webui.py`、`src/core/main.py`

### B7. 监听地址与局域网开关（必需）

- 浏览器版：`--host` 默认 `0.0.0.0`（现状，见 `main.py:135-139`），局域网直接可用
- 桌面版：端口固定 `8317`，host 由 Electron 按开关**在启动时**传入：

  | 开关 | 传入参数 | 实际监听 |
  |---|---|---|
  | 关闭（默认） | `--host 127.0.0.1 --port 8317` | 仅本机 |
  | 开启 | `--host 0.0.0.0 --port 8317` | 本机 + 局域网 |

- host **不作为后端可改配置项**、不写进 `settings.json`：它属于「桌面壳的启动参数」，
  由 `desktop-config.json` 的 `lan_enabled` 决定
- 保存开关后不热切换，提示「重启后生效」，Electron 侧 `app.relaunch()` 完成重启
- 开启后 Electron **仍然加载 `http://127.0.0.1:8317`**，所以渲染进程 origin 不变，
  localStorage 里的偏好与会话不受影响
- 首次开启时 Windows 会弹防火墙授权（针对 Python 可执行文件），需要在 UI 文案里预告，
  并提示「若误点取消，局域网将无法访问」
- `/api/health` 增加 `host`、`lan_urls`（复用 `webui.py` 已有的 `_lan_addresses()`），
  供设置页展示地址与二维码

**落点**：`src/core/webui.py`（`serve_forever` 取实际绑定地址、health 返回 lan_urls）、
`src/core/main.py`（`--desktop` 下的 host 断言）

### B8. CORS（不需要）

同源方案下无需 CORS。仅当将来支持「多窗口分端口」或「外部工具调用 API」时才需要，
届时再加显式白名单。

---

## 5. 前端改造清单

### F1. 桌面适配（小改）

| 项 | 做法 | 落点 |
|---|---|---|
| 无边框标题栏 | 顶部条 `-webkit-app-region: drag`，按钮区 `no-drag`；窗口控制按钮仅在 Electron 下渲染 | `App.tsx` + `global.css` |
| 外链 | 拦截 `window.open` / `<a target=_blank>`，交给系统浏览器 | 主进程 `setWindowOpenHandler` |
| 系统通知 | 任务完成且窗口未聚焦时通知 | `App.tsx` 收尾回调 |
| 原生文件选择 | 桌面下「打开文件夹」调原生对话框；浏览器保留现有上传流程 | `api.ts` / `WorkbenchFiles.tsx` |
| 拖入文件取真实路径 | 桌面下用 `webUtils.getPathForFile`，省掉一次上传复制 | `Chat.tsx` |
| 离线提示 | 后端未就绪时展示启动中/失败面板，而不是白屏 | 新增 `DesktopBoot.tsx` |

以上通过 `window.__DESKTOP__`（preload 注入）判断，浏览器下一律走原路径。

### F2. localStorage 保持不动（决策 D3 + D4）

会话 id、模式、确认档位、工作区、面板宽度都存在 localStorage（`App.tsx:55-75,126-162`、
`Workbench.tsx:48`、`Trajectory.tsx:62`），而 localStorage 按 **origin（含端口）** 隔离。

结论：**不改造，继续用 localStorage**，代价是必须保证桌面端 origin 恒定 →
固定 `127.0.0.1:8317`，不允许用户改端口。

- 不做「端口被占用时回退随机端口」：静默换端口会让用户悄悄丢掉当前会话和全部偏好
- 端口被占用时的正确行为是**启动失败**，由 Electron 展示明确提示（见 6.2 失败分支）
- 浏览器版固定 `:8000`，本来就不受影响
- 浏览器 `:8000` 与桌面 `:8317` 是两个 origin，会话的「当前选中项」不共享；
  会话数据本身在后端共享，所以切过去只需在列表里重新点一次，不算缺陷

### F3. 局域网访问（两端都支持，入口不同）

**桌面端**：系统配置弹窗新增「网络」分组，仅在 `window.__DESKTOP__` 下渲染

| 元素 | 说明 |
|---|---|
| 开关「允许局域网访问」 | 默认关闭；值来自 `desktop-config.json`（经 IPC 读写，不走 `/api/settings`） |
| 生效提示 | 切换后出现提示条：**「已保存，重启应用后生效」** + `[立即重启]` `[稍后]` |
| 当前监听 | 显示 `127.0.0.1:8317` 或 `0.0.0.0:8317`，并标注「仅本机 / 局域网可访问」 |
| 局域网地址 + 二维码 | 开关已开启时展示 `/api/health` 返回的 `lan_urls`，二维码内含 `?k=<token>`，手机扫码即进 |
| 安全提示 | 开启时明确告知「同一网络的设备可访问并操作本机文件，请确认当前网络可信」 |
| 防火墙说明 | 首次开启提示「Windows 可能询问防火墙权限，请选择允许专用网络」 |

**浏览器端**：设置页同样展示「局域网地址 + 二维码」，数据源同样是 `/api/health`，
但**不出现开关**（监听地址由启动参数决定，浏览器模式下没有重启入口）。

其余不变：

- 手机端已有窄屏适配（抽屉浮层、工具栏折叠），无需重做
- 远端模式下已隐藏新建/改名/删除工作区（`Sidebar.tsx` 的 `remote` 分支），保持不变
- 上传面板在移动端依赖 `label` 原生激活（`Chat.tsx:1188-1192`），Electron 不受影响

---

## 6. Electron 壳设计

### 6.1 目录结构

```
desktop/
├── package.json            # electron + electron-builder（与 web/package.json 分离）
├── electron.vite.config.ts # 可选：用 electron-vite 打包主进程/preload
├── src/
│   ├── main.ts             # 生命周期、spawn 后端、单实例、托盘、重启退避
│   ├── backend.ts          # 后端进程管理：spawn / READY 解析 / 退出回收 / 日志转发
│   ├── preload.ts          # 白名单 IPC
│   └── config.ts           # desktop-config.json 读写（lan_enabled、数据目录、窗口尺寸；端口固定不存）
└── build/                  # 图标、签名资源、安装器配置
```

### 6.2 启动时序

```
app.whenReady
  → 单实例锁（第二次启动只 focus 已有窗口）
  → 读 config（lan_enabled、dataDir、窗口尺寸）
  → 按 lan_enabled 决定 host：关闭 → 127.0.0.1 / 开启 → 0.0.0.0
  → spawn 后端（--desktop --host <host> --port 8317 --data-dir <userData>）
  → 监听 stdout 逐行，匹配 MINI_AGENT_READY（超时 30s 视为失败）
  → 创建 BrowserWindow（show:false）→ loadURL(http://127.0.0.1:8317/?k=token)
  → did-finish-load 后 show（避免白屏）
  → 失败：显示诊断面板（后端日志尾部 + 重试 / 打开日志目录）
app.on('before-quit') → POST /api/shutdown → 等 3s → kill → 退出
```

> 即使 `lan_enabled=true`，窗口也始终加载 `127.0.0.1:8317`，保证渲染进程 origin 恒定。

**端口占用是独立分支，必须给出可操作的提示**（决策 D4 的直接后果）：

```
收到 MINI_AGENT_ERROR {"code":"PORT_IN_USE","port":8317}
  → 不重试、不换端口
  → 弹出对话框：
    标题：启动失败：端口 8317 已被占用
    正文：另有一个程序正在使用 127.0.0.1:8317，mini-agent 无法启动。
          常见原因：上一次的 mini-agent 后端没有正常退出。
    按钮：[复制排查命令]  [退出]
  → 「复制排查命令」写入剪贴板：
      Windows:  netstat -ano | findstr :8317    然后 taskkill /F /PID <PID>
      macOS/Linux: lsof -i :8317
```

Electron 启动前也可先自行探测一次 8317（`net.connect`），把错误提前到窗口出现之前，
避免用户看到「先白屏再报错」。

单实例锁、`window-all-closed` 行为、托盘常驻（可选「关闭到托盘」）都要显式定义。

### 6.3 安全基线

```ts
new BrowserWindow({
  webPreferences: {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    preload: path.join(__dirname, "preload.js"),
  },
})
```

- 只允许加载 `http://127.0.0.1:<port>`，禁止导航到外部域名（`will-navigate` 拦截）
- 不给渲染进程任何 Node 能力；IPC 只暴露：`openExternal`、`pickFolder`、`revealInFolder`、
  `getAppInfo`、`restartBackend`
- 生产构建禁用 DevTools 快捷键（可用配置项打开）

### 6.4 IPC 白名单

| 通道 | 方向 | 用途 |
|---|---|---|
| `app:info` | invoke | 版本、数据目录、是否桌面端 |
| `shell:openExternal` | invoke | 打开外链 |
| `dialog:pickFolder` | invoke | 选工作区目录 |
| `shell:reveal` | invoke | 在资源管理器中显示 |
| `backend:retry` | invoke | 启动失败后重新拉起后端（端口问题由用户自行处理后） |
| `backend:status` | event | 后端启动/崩溃状态推给渲染层 |
| `config:get` | invoke | 读桌面配置（`lan_enabled`、是否桌面端、版本） |
| `config:setLan` | invoke | 写 `lan_enabled`，返回 `{restartRequired:true}` |
| `app:relaunch` | invoke | 立即重启应用（`app.relaunch()` + `app.exit(0)`） |

---

## 7. 打包与分发

### 7.1 Python 侧

- **PyInstaller `--onedir`**（不用 onefile：启动快、杀软误报少、便于 diff 更新）
- 必须显式收集的动态内容（否则打包后工具全没）：
  - `plugins/`、`skills/`、`mcp/`（运行时扫描目录，静态分析抓不到）
  - `web/dist`（若不由 Electron 侧单独分发）
  - `rapidocr_onnxruntime` 的模型文件与 `onnxruntime` 的 DLL（PyInstaller hook 可能不全，
    需 `--collect-data` / `--collect-binaries` 验证）
  - `pypdfium2` 的二进制
- 内置可选依赖（决策 D1，**整体打包**）：`docs` + `docs-fast` + `dataframe` + `ocr`
  全部进同一个安装包；不提供「标准版 / 完整版」，也不做首次启动按需下载
- 入口：`core/main.py`，需要把 `sys.stdout` 设为行缓冲/无缓冲，保证 READY 行及时输出

体积估算（待打样确认，仅作规划用）：

| 组成 | 粗估 |
|---|---|
| Python 运行时 + openai + 核心 | 40–60 MB |
| docs / docs-fast（docx、openpyxl、pdfplumber、reportlab、calamine…） | 40–60 MB |
| OCR（pypdfium2 + rapidocr + onnxruntime） | ~170 MB |
| Electron 运行时 | 80–100 MB |
| 前端 dist | 5–10 MB |
| **合计（安装包压缩后）** | **约 200–300 MB** |

> OCR 的 `onnxruntime` 已锁 `<1.21`（见 `pyproject.toml` 注释），打包时不要被
> 依赖解析覆盖。

### 7.2 Electron 侧

- `electron-builder`，Windows 产 `nsis`（用户级安装，无需管理员）+ `portable` 备选
- 后端 onedir 作为 `extraResources/server/`
- 安装目录只读；所有可写数据在 `%APPDATA%/mini-agent`
- 首次启动写入默认 `desktop-config.json`，并复制示例插件/技能到数据目录（若没有）
- 代码签名（Authenticode）避免 SmartScreen 拦截；无签名时安装包会被拦，需在文档说明
- 自动更新：`electron-updater`；后端二进制与前端 dist 必须与新版本一起替换
  → 建议整包更新，不做增量

### 7.3 构建矩阵

- Windows / macOS / Linux 需各自构建（PyInstaller 不能交叉编译）
- 先做 Windows，macOS 需要签名 + 公证，Linux 可选 AppImage
- CI 建议：`windows-latest` 走完整链路，产物上传 artifact

---

## 8. 配置项汇总（新增）

| 配置 | 形式 | 默认 | 说明 |
|---|---|---|---|
| `MINI_AGENT_DATA_DIR` / `--data-dir` | 环境变量 / 参数 | `ROOT`（浏览器） / `%APPDATA%/mini-agent`（桌面） | 可写数据根 |
| `MINI_AGENT_WEB_DIST` | 环境变量 | `ROOT/web/dist` | 前端静态资源 |
| `--port` | 参数 | 浏览器 8000 / 桌面固定 8317 | 桌面端由 Electron 固定传入，不暴露给用户 |
| `--desktop` | 参数 | 关 | 桌面模式：输出 READY/ERROR 行，端口固定 8317 |
| `--no-auth` | 参数 | 关 | 关闭 token 校验（仅调试/测试） |
| `lan_enabled` | Electron `desktop-config.json` | `false` | 桌面版是否允许局域网访问；**重启生效** |

- `--host` 对浏览器版保持现状（默认 `0.0.0.0`）；`--desktop` 时由 Electron 依据
  `lan_enabled` 显式传入 `127.0.0.1` 或 `0.0.0.0`，后端不做二次推断
- `lan_enabled` 只存在于桌面壳配置中，**不进 `settings.json`**、不出现在浏览器模式的设置页

---

## 9. 分阶段实施计划

### 阶段 1：后端可桌面化（不动 UI）

- B1 固定端口 8317 + READY 握手 + `PORT_IN_USE` 错误行
- B2 数据目录分离（`DATA_DIR`）
- B3 静态资源路径可配
- B5 `/api/health`
- B6 优雅退出 + `/api/shutdown`

**验收**：`--desktop --data-dir <tmp>` 启动后 stdout 出现 READY 且端口为 8317；写入配置落在
`<tmp>`；`curl /api/health` 正常；先占用 8317 再启动，stdout 出现 `PORT_IN_USE` 且退出码非 0；
发 SIGTERM 后无残留子进程与 MCP server；`pytest tests -q` 全绿。

### 阶段 2：鉴权与网络策略

- B4 token + Cookie 鉴权 + Host 校验
- B7 `--desktop` 按传入 host 绑定；`/api/health` 回传 `host` / `lan_urls`
- 测试适配（`--no-auth` 或读取 token）

**验收**：不带 token 请求 `/api/*` 返回 401；带 `?k=` 首次访问后 Cookie 生效；
`Host` 伪造被拒；浏览器生产模式行为与现在一致（除多了鉴权）；
`--desktop --host 127.0.0.1` 时 `netstat` 只监听本机，`--host 0.0.0.0` 时可见 `0.0.0.0:8317`。

### 阶段 3：Electron 壳跑通

- `desktop/` 骨架：spawn → READY → `loadURL` → 退出回收
- 单实例、崩溃重启退避、后端日志落盘
- 端口占用失败分支（对话框 + 复制排查命令 + 启动前自探测）
- F2 固定端口，偏好沿用 localStorage
- `desktop-config.json` 读写 + `lan_enabled` 决定 spawn 的 `--host`

**验收**：双击启动 10 秒内出主界面；关闭窗口后无残留进程；连续启动 5 次会回到上次会话
（localStorage 未丢）；手动占用 8317 后启动，出现「端口被占用」对话框而不是白屏或自动换端口；
手动把 `lan_enabled` 改为 `true` 后重启，`netstat` 显示 `0.0.0.0:8317`。

### 阶段 4：桌面体验

- F1 全部项（标题栏、外链、通知、原生文件选择、拖拽路径、启动面板）
- 托盘菜单（显示 / 停止任务 / 日志目录 / 退出）
- F3 设置页「网络」分组：局域网开关 + 重启生效提示 + 地址与二维码（桌面端带开关，
  浏览器模式只展示地址与二维码）
- `app.relaunch()` 立即重启链路

**验收**：桌面窗口拖动与按钮响应正常，外链在系统浏览器打开，任务完成有通知；
勾选「允许局域网访问」→ 提示重启 → 点「立即重启」→ 手机扫码可正常聊天并打开工作区文件；
取消勾选后重启，手机侧连接立即失效；浏览器模式下同样能看到二维码但无开关。

### 阶段 5：打包分发

- PyInstaller onedir + 内置 docs / docs-fast / dataframe / OCR（单包）
- electron-builder NSIS + portable
- 首次启动初始化数据目录与示例插件
- 签名与自动更新
- 文档：`README` 新增「桌面版」章节、`docs/DESKTOP.md`

**验收**：在**未装 Python / Node** 的干净 Windows 虚拟机上：
安装 → 启动 → 配置模型 Key → 对话 → 上传图片做 OCR → 读写 docx / xlsx → 卸载后残留仅数据目录。

---

## 10. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| PyInstaller 漏收动态插件/模型文件 | 打包后工具缺失、OCR 报错 | 每次打包跑冒烟脚本：列工具数、跑一次 OCR、读写 docx |
| 单包体积约 200–300 MB（OCR 占 ~170 MB） | 下载与安装耗时、占用磁盘 | 已决策整体打包；在下载页标注体积与所需空间，安装器显示解压进度 |
| 端口 8317 被占用 | 无法启动 | 不做回退；结构化 `PORT_IN_USE` + 对话框提示 + 一键复制 `netstat` 排查命令；Electron 启动前先自探测 |
| 开启局域网后攻击面扩大 | 同网段设备可访问 API（能读写文件、执行命令） | 默认关闭；开启时强提示「请确认当前网络可信」；`lan_enabled=true` 时**强制启用 token 鉴权**（忽略 `--no-auth`）；`Host` 头校验 |
| Windows 防火墙拦截 | 手机连不上，用户以为功能坏了 | 首次开启时预告授权弹窗；连接失败时给出「检查防火墙专用网络规则」的提示 |
| 用户忘记自己开过局域网 | 长期暴露在公共网络 | 主窗口顶栏常驻「局域网已开启」提示条，点击可跳转设置关闭 |
| 上次后端未正常退出导致端口残留 | 用户以为「打不开」 | 单实例锁 + 优雅退出（B6）+ 提示文案里直接说明这一常见原因 |
| 固定端口与另一个应用冲突 | 无法启动（本方案接受） | 文档注明 8317 为独占端口；如需共存必须改源码常量 |
| token 鉴权破坏现有测试 | CI 红 | 统一测试开关；测试读取 READY token |
| 杀软误杀 PyInstaller 产物 | 无法安装 | 代码签名；改用 onedir；必要时上报白名单 |
| macOS 公证 | 无法分发 | 先 Windows，macOS 单独立项 |

## 11. 决策记录

以下问题已确认，不再作为开放项：

| # | 问题 | 结论 |
|---|---|---|
| D1 | OCR / 文档依赖怎么分发 | 整体打包，单安装包，不按需下载 |
| D2 | 桌面端是否开局域网 | **提供开关，默认关闭，重启应用后生效**；开启后窗口仍加载 `127.0.0.1` |
| D3 | 界面偏好存哪里 | 继续 localStorage，不上移后端 |
| D4 | 桌面端口 | 固定 `8317`，不可修改；占用即启动失败并提示 |
