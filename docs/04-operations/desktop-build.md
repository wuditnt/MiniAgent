# 桌面端打包（Windows / Electron）

## 1. 用途

把 mini-agent 打成一个可以双击运行的 Windows 桌面应用：

- 界面：Electron 加载后端托管的同一套 Web UI（`http://127.0.0.1:8317`），前端零改造、同源请求
- 能力：安装包内置**真实的 Python 解释器**和全部依赖（OCR 依赖打包为独立的 `ocr-pack.zip`，首次使用时自动解压）
- 工具：`run_python` / MCP server / `run_shell` 里的 `python` 全部指向内置解释器，不是冻结后的假解释器

产物是一个 NSIS 安装包（约 224MB），以及一份解压即用的 `win-unpacked` 目录。

## 2. 概念

### 2.1 两段式构建

```
web/src  ──npm run build──▶  web/dist
src/ tools/ skills/ docs/ ─┐
web/dist                   ├─ build_desktop.py ─▶ build/desktop-resources/app/
可移植 Python + 依赖 ──────┘                      build/desktop-resources/python/
                                                        │
                                              electron-builder
                                                        ▼
                                       desktop/release/*.exe（NSIS 安装包）
```

`desktop/release/win-unpacked/` 是免安装的完整目录，可直接运行验证。

### 2.2 内置 Python 是怎么来的

不冻结、不下载 embeddable 包，而是**复制一棵能直接运行的 Python 安装树**：

| 内容 | 来源 | 说明 |
|---|---|---|
| 解释器本体 | 运行构建脚本的解释器的 `sys.base_prefix` | Windows 的 Python 安装按相对路径找 DLL / `Lib`，整目录搬走即可用 |
| 基础依赖 | 优先 `<仓库>/.venv/Lib/site-packages` | venv 本身不可迁移，只搬它的包 |
| 裁剪 | `Lib/test`、`Lib/idlelib`、`Lib/turtledemo`、`Lib/lib2to3`、`Doc`、`Tools`、`Scripts`、`_polars_runtime_32` | 纯瘦身，不影响运行 |
| 开发工具剔除 | `pipreqs`、`IPython`、`jedi`、`pygments`、`tornado`、`prompt_toolkit`、`pytest` 等 | 运行时无人 import，却占 7 千个文件（见 `SITE_SKIP`） |
| OCR 依赖外置 | `cv2`、`onnxruntime`、`sympy`、`numpy`、`shapely`、`pyclipper`、`PyYAML` 等 | 打进 `ocr-pack.zip`，首次使用 OCR 才解压（见 2.3） |

`tcl/` 必须保留：后端「选择文件夹」用的 tkinter 依赖它。

### 2.3 按需 OCR 组件包

OCR 依赖约 230MB、近 3 千个文件，但只有 `ocr_image` / `ocr_pdf` 用得到。安装耗时主要花在逐个小文件写盘上，所以这部分不进 `site-packages`：

- 构建时把相关条目（`OCR_PREFIXES`）摊到暂存目录，压成 `resources\ocr-pack.zip`（约 90MB）
- 安装阶段只写这 1 个文件，OCR 库不参与解压铺开
- 首次调用 OCR：`core/extra_libs.py` 把包解压到 `%APPDATA%\mini-agent\pylibs\ocr`（约 2–4 秒），注入 `sys.path`，并同步 `PYTHONPATH` 供 `run_python` 等子进程继承
- 之后按「zip 大小 + mtime」判定缓存，命中时开销约 1ms
- 包不存在（源码运行 / 浏览器版）时该模块什么都不做，依赖本来就在解释器里

### 2.4 运行期约定

- 端口固定 `8317`（决策 D4）：origin 恒定，localStorage 里的会话与偏好才不会丢；被占用时**启动失败**，不换端口
- 默认只监听 `127.0.0.1`；局域网开关（`desktop-config.json` 的 `lan_enabled`）由 Electron 在启动时翻译成 `--host 0.0.0.0`
- 安装目录只读，可写数据在 `%APPDATA%\mini-agent`（配置 / 会话 / 账号库 / 日志）
- 后端启动成功后向 stdout 输出 `MINI_AGENT_READY {...}`，Electron 解析到这一行才开始加载页面

## 3. 使用方式

### 3.1 前置条件

- Windows x64
- Node.js 18+
- Python 3.12，且 `.venv` 已按 `requirements.txt` 装好（打包取的是它的依赖）

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
cd web; npm install; cd ..
```

### 3.2 构建前端

```powershell
cd web
npm run build          # 产物 web\dist
cd ..
```

`build_desktop.py` 发现 `web\dist\index.html` 不存在时会自动调用 `npm run build`；已有产物则跳过。

### 3.3 准备打包资源

```powershell
.venv\Scripts\python.exe scripts\build_desktop.py
```

- 推荐用 `.venv` 的解释器跑：解释器版本与依赖来源天然一致
- 用系统 `python` 也能跑，但若系统 Python 与 `.venv` 大版本不同，会得到「新解释器 + 旧 ABI 的 wheel」，脚本的依赖校验会中断构建
- 跳前端检查：`--skip-web`

脚本最后会做三段校验，任一失败直接非 0 退出：

1. 用打包后的 `python.exe` 真实 import 基础依赖（`openai / docx / openpyxl / PIL / pypdf / tkinter / pdfplumber / polars / reportlab / pypdfium2 / pptx`）
2. 反向确认 OCR 库已不在基础依赖里（`find_spec` 应为 None）
3. 用暂存目录冒充已解压状态，验证 OCR 组件包自身完整（`cv2 / numpy / shapely / pyclipper / yaml / google.protobuf / onnxruntime / rapidocr_onnxruntime`）

### 3.4 安装 Electron 依赖（首次）

```powershell
cd desktop
npm install            # 会下载约 115MB 的 Electron
```

### 3.5 打包

```powershell
npm run dist
```

产物：

| 文件 | 说明 |
|---|---|
| `desktop/release/mini-agent Setup 1.0.0.exe` | NSIS 安装版，可选安装目录、创建桌面快捷方式 |
| `desktop/release/win-unpacked/` | 未压缩目录，调试首选 |

只要未压缩目录：

```powershell
npx electron-builder --win --dir
```

### 3.6 开发运行（不打包）

```powershell
cd desktop
npm start              # tsc 编译后用 electron . 启动，复用仓库 .venv
```

### 3.7 改完代码重新打包

改了后端 / 工具 / 技能 / 前端：

```powershell
.venv\Scripts\python.exe scripts\build_desktop.py --skip-web   # 改了前端则先 npm run build
cd desktop; npm run dist
```

**只跑 `npm run dist` 不会更新后端**——`src/` 是由 `build_desktop.py` 拷进安装包的。

## 4. 关键代码位置

| 位置 | 作用 |
|---|---|
| `scripts/build_desktop.py` | 准备 `app/`、`python/` 与 `ocr-pack.zip`；`APP_ITEMS` 控制随包内容，`PY_SKIP_*` / `SITE_SKIP` 控制裁剪，`OCR_PREFIXES` 决定哪些包移入组件包 |
| `desktop/package.json` | electron-builder 配置：`extraResources` 把 `build/desktop-resources/{app,python,ocr-pack.zip}` 映射到安装目录 `resources/` |
| `desktop/src/main.ts` | 生命周期、单实例、读配置、启动失败诊断框、退出时关后端 |
| `desktop/src/backend.ts` | spawn 内置 Python、解析 READY/ERROR、`/api/shutdown` + `taskkill` 回收进程树 |
| `desktop/src/config.ts` | `%APPDATA%\mini-agent\desktop-config.json` 读写、数据/日志目录 |
| `desktop/src/preload.ts` | `window.__DESKTOP__` 白名单桥（前端据此显示桌面专属 UI） |
| `src/core/main.py` | `--desktop`、固定端口与 `--host` 校验 |
| `src/core/extra_libs.py` | 解压 `ocr-pack.zip` 到数据目录并注入 `sys.path` / `PYTHONPATH`（`MINI_AGENT_EXTRA_PACK` / `MINI_AGENT_EXTRA_LIBS`） |
| `src/core/webui.py` | `MINI_AGENT_READY / MINI_AGENT_ERROR` 握手、`/api/health` 的 `mode / host / lan_urls` |

安装后的目录结构：

```
安装目录\
├── mini-agent.exe
└── resources\
    ├── app\                 src、builtin_tools、skills、mcp、docs、web\dist、README
    ├── python\              python.exe + Lib\site-packages（基础依赖，不含 OCR）
    ├── ocr-pack.zip         OCR 组件包，首次使用 OCR 时解压到数据目录
    └── app.asar             Electron 主进程 / preload
```

## 5. 注意事项

### 5.1 winCodeSign 解压失败（最常见）

现象：`Cannot create symbolic link ... darwin\10.12\lib\libcrypto.dylib`，重试 4 次后失败。
原因：缓存压缩包里含 macOS 符号链接，Windows 非管理员 / 未开发者模式无法创建。
处理：手动预置缓存（只需一次，跳过 `darwin` 目录）：

```powershell
$cache = "$env:LOCALAPPDATA\electron-builder\Cache\winCodeSign"
New-Item -ItemType Directory -Force $cache | Out-Null
curl.exe --ssl-no-revoke -L -o "$env:TEMP\winCodeSign.7z" `
  https://github.com/electron-userland/electron-builder-binaries/releases/download/winCodeSign-2.6.0/winCodeSign-2.6.0.7z
& "desktop\node_modules\7zip-bin\win\x64\7za.exe" x "$env:TEMP\winCodeSign.7z" `
  "-o$cache\winCodeSign-2.6.0" "-x!darwin" -y
```

### 5.2 下载慢或超时

```powershell
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
```

`curl.exe` 若报证书吊销检查失败，加 `--ssl-no-revoke`。

### 5.3 体积

实测（2026-11，Electron 33 / Python 3.12）：

| 项 | 体积 | 文件数 |
|---|---|---|
| `resources/app` | 9.8 MB | 244 |
| `resources/python` | 175.9 MB | 9,522 |
| `resources/ocr-pack.zip` | 89.2 MB | 1 |
| 安装前 payload 合计 | 274.9 MB | 9,767 |
| NSIS 安装包 | 223.7 MB | — |

对比优化前：payload 720MB / 19,933 文件、安装包 210.6MB。安装包体积略增（zip 已压缩，NSIS 无法再压缩），但安装时写盘量和文件数分别下降 62% 和 51%，这才是安装耗时的决定因素。

- 继续瘦身：在 `build_desktop.py` 的 `PY_SKIP_LIB` / `PY_SKIP_EXTRA` 增加目录，或把更多包加进 `OCR_PREFIXES`，但必须保证 `verify_python()` 全绿
- 不要裁剪 `tcl/`：会丢掉原生「选择文件夹」对话框

### 5.4 版本一致性

- 内置解释器版本 = 构建机 Python 版本，当前为 3.12；换大版本必须重新打包并确认依赖 ABI 匹配
- 只支持 Windows x64；要出 mac/linux 需要另建资源目录与签名流程

### 5.5 其他已知限制

- 安装包未签名（首次运行有 SmartScreen 提示）、暂用 Electron 默认图标
- 桌面版仍要求登录（沿用 `auth.db`），与浏览器版共享同一套账号体系，但数据目录不同（`%APPDATA%\mini-agent` vs `~/.mini-agent`），首次启动是空库
- 端口 `8317` 被占用时启动失败并给排查命令（`netstat -ano | findstr :8317`），这是刻意设计，不做随机端口回退
- 首次使用 OCR 会解压组件包（约 2–4 秒）；解压目录在 `%APPDATA%\mini-agent\pylibs\ocr`，可直接删除以强制重建
