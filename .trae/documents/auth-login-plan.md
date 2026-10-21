# 登录与账号体系方案（本地 SQLite）

> 状态：**已实现**（阶段 1–4）。阶段 5（与 Electron token 串联）等桌面方案启动后再做。
> 已确认：数据隔离先做 A；默认密码 `admin/admin123` + 强制改密；两档角色；仅超管建号。
> 目标：新增登录页；内置一个超级管理员 `admin`；超管可增删改账号；账号数据存本地 SQLite。
> 与已定的 Electron / 局域网方案衔接，但不合并成一层。

---

## 1. 先说清楚三个必须先定的问题

这三个决定直接决定工程量，建议在动手前确认。

### Q1：只做「登录」，还是做「数据隔离」？

当前所有数据都是全局的：会话列表、工作区、模型配置、记忆，没有归属概念。

| 方案 | 内容 | 工作量 | 后果 |
|---|---|---|---|
| **A. 只做登录（建议先做）** | 账号只用于区分「谁在登录」；所有人看到同一份会话与工作区 | 小 | 多账号更像「多人共用一个工作台」 |
| B. 按账号隔离 | 给 session / workspace / model 加 `owner_id`，列表按人过滤，老数据迁移 | 大（约 A 的 2–3 倍） | 每个账号有自己的会话与工作区，符合直觉 |

本方案按 **A** 设计，同时在数据模型里预留 `owner_id` 的扩展位，后续做 B 不需要重建表。

### Q2：默认密码怎么给？

| 方案 | 说明 |
|---|---|
| **A. 固定 `admin` / `admin123` + 首次登录强制改密（建议）** | 用户能预期、文档好写；靠 `must_change_password=1` 保证不会被长期沿用 |
| B. 首次启动随机生成并打印到控制台 | 更安全，但桌面版用户看不到控制台，容易「装完就进不去」 |

选 A，但**必须强制改密**：未改密前除 `/api/auth/password` 外所有 API 返回 403。

### Q3：登录与 Electron 的 token 是什么关系？

两者不是一回事，需要共存而不是合并：

| 层 | 回答的问题 | 载体 |
|---|---|---|
| 传输层 token（原 B4） | 「这个客户端有资格访问本机服务吗」 | URL `?k=` → Cookie，Electron / 二维码自动注入 |
| 登录会话 | 「你是哪个账号」 | `ma_session` Cookie |

**不能合并**：如果「扫码即登录」，那么同一 WiFi 下任何人扫到二维码就等于拿到超管权限。
所以手机扫码后仍要输一次账号密码。

---

## 2. 数据模型

文件位置：`<DATA_DIR>/web_data/auth.db`（与 `settings.json` 同目录，跟随 `--data-dir`）。

```sql
-- 账号
CREATE TABLE IF NOT EXISTS users (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  username             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  password_hash        TEXT    NOT NULL,          -- scrypt$n$r$p$salt$hash
  display_name         TEXT    NOT NULL DEFAULT '',
  role                 TEXT    NOT NULL DEFAULT 'user'
                       CHECK (role IN ('superadmin', 'user')),
  disabled             INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT    NOT NULL,
  updated_at           TEXT    NOT NULL,
  last_login_at        TEXT
);

-- 登录会话（服务端保存，可强制下线）
CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT    PRIMARY KEY,                -- sha256(token)，不存原文
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT    NOT NULL,
  expires_at  TEXT    NOT NULL,
  last_seen_at TEXT   NOT NULL,
  ip          TEXT    NOT NULL DEFAULT '',
  user_agent  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- 登录失败记录（用于限流；也可只放内存，重启即清）
CREATE TABLE IF NOT EXISTS login_failures (
  username     TEXT    NOT NULL,
  ip           TEXT    NOT NULL DEFAULT '',
  failed_at    TEXT    NOT NULL,
  PRIMARY KEY (username, ip, failed_at)
);
```

设计说明：

- `token_hash` 存哈希：数据库文件被拷走也无法重放会话
- `role` 只有两档：`superadmin` 管账号，`user` 只能用功能；不引入细粒度权限
- `disabled` 与删除分离：删号会让历史归属悬空，禁用更安全
- 预留扩展：后续做数据隔离时给相关表加 `owner_id`，本表不用改

---

## 3. 密码与安全

### 3.1 哈希

用标准库 `hashlib.scrypt`，不引入新依赖：

```
scrypt$16384$8$1$<salt_hex>$<hash_hex>
n=2^14, r=8, p=1, salt=16B, dkLen=32
```

- 参数写进字符串，后续调参可平滑升级（校验时按记录里的参数算）
- 比较用 `hmac.compare_digest`，避免时序侧信道
- 密码长度限制 8–128，不做复杂度硬性要求（本地工具，易用优先）

### 3.2 防暴力破解

- 同用户名 + 同 IP：连续 5 次失败 → 锁 5 分钟
- 用户名不存在时也执行一次假哈希校验（固定 dummy hash），避免通过响应时间枚举账号
- 错误文案统一为「用户名或密码错误」，不区分「用户不存在 / 密码错误」

### 3.3 会话 Cookie

```
Set-Cookie: ma_session=<32B hex>; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800
```

- 有效期 7 天，滑动续期：剩余不足 1 天时延长
- 不设 `Secure`（本地是 http）；若将来上 HTTPS 再加
- 改密时删除该用户的其他会话（保留当前）
- 启动时清理 `expires_at < now` 的会话

### 3.4 CSRF

`SameSite=Lax` + 只接受 `Content-Type: application/json` + `Host` 白名单，三者叠加足够：

- `<form>` 跨站提交无法设置 `application/json`，会被后端拒绝
- 配合原方案的 Host 校验，挡住 DNS rebinding
- 不额外引入 CSRF token（本地单机场景收益低、改动面大）

---

## 4. 后端实现

### 4.1 模块划分

| 文件 | 职责 |
|---|---|
| `src/core/auth.py`（新增） | 纯逻辑：建库、密码哈希、用户 CRUD、会话签发/校验、限流 |
| `src/core/webui.py`（修改） | 路由与守卫：`/api/auth/*`、`/api/users*`，请求入口统一鉴权 |
| `src/core/main.py`（修改） | 新增 `--no-login`（测试用）、`--reset-password`（救援用） |

### 4.2 SQLite 与多线程

`ThreadingHTTPServer` 是一请求一线程，SQLite 连接不能跨线程共享：

- 用 `threading.local()` 每线程一个 connection
- 连接参数：`PRAGMA journal_mode=WAL`、`PRAGMA busy_timeout=5000`、
  `PRAGMA foreign_keys=ON`
- 写操作用 `with conn:` 事务包裹
- 建表与初始化在 `serve()` 启动时执行一次（`CREATE TABLE IF NOT EXISTS` 幂等）

### 4.3 路由守卫

在 `do_GET` / `do_POST` 的**最前面**统一判定，避免每个 handler 各写一遍：

```
白名单（不需要登录）：
  POST /api/auth/login
  GET  /api/health
  静态资源（/、/assets/*、/favicon.ico 等）

其余 /api/* 一律要求有效 ma_session
未登录 → 401 {"error":"未登录"}
已登录但 must_change_password=1 → 403 {"error":"请先修改初始密码","code":"MUST_CHANGE_PASSWORD"}
```

与 Electron token 的关系：token 校验在前（传输层），登录校验在后（身份层），两者独立。

### 4.4 接口清单

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 匿名 | `{username, password}` → 设 Cookie，返回 `{user}` |
| POST | `/api/auth/logout` | 登录 | 删除当前会话 |
| GET | `/api/auth/me` | 登录 | 返回当前用户（前端启动时判断是否已登录） |
| POST | `/api/auth/password` | 登录 | `{old_password, new_password}`，改完清 `must_change_password` |
| GET | `/api/users` | 超管 | 账号列表（含 role、disabled、last_login_at，不返回哈希） |
| POST | `/api/users` | 超管 | `{username, password, display_name, role}` |
| PATCH | `/api/users/<id>` | 超管 | 改显示名 / 角色 / 启用状态 / 重置密码 |
| DELETE | `/api/users/<id>` | 超管 | 删除账号 |
| POST | `/api/users/<id>/logout-all` | 超管 | 强制该账号下线（删其全部会话） |

### 4.5 超管操作的保护规则

- 不能删除自己
- 不能禁用自己
- 不能把最后一个超管降级或删除（至少保留 1 个启用状态的 superadmin）
- 用户名大小写不敏感唯一（`COLLATE NOCASE`）
- 新建账号默认 `must_change_password=1`（超管给的是初始密码）

---

## 5. 前端实现

### 5.1 登录页

- 新增 `web/src/components/LoginPage.tsx`，整屏居中卡片（与现有 AntD 风格一致）
- 字段：用户名、密码；错误提示统一「用户名或密码错误」；加载态禁用按钮
- 成功：写入内存态用户信息 → 渲染主界面（不整页刷新，避免丢 SSE 状态）
- 记住用户名（localStorage，**不存密码**）

### 5.2 启动流程

`main.tsx` 增加一层 `AuthGate`：

```
挂载 → GET /api/auth/me
  ├─ 200 → <App user={...} />
  ├─ 401 → <LoginPage />
  └─ 403 MUST_CHANGE_PASSWORD → <ForceChangePassword />（不可跳过）
```

这样无需改 `App.tsx` 的内部状态，登录成功后再挂载主界面，避免未登录时先触发一堆 `/api/*` 请求。

### 5.3 401 全局处理

`api.ts` 里加统一拦截：任意请求返回 401 → 清空当前用户 → 切回登录页。
SSE（`/api/chat`）是 `fetch` 流式读取，Cookie 同源自动携带，无需改协议。

### 5.4 账号管理

- `SettingsModal` 新增「账号管理」页签，仅 `role === "superadmin"` 时渲染
- 表格列：用户名、显示名、角色、状态、最近登录、操作
- 操作：重置密码（弹窗输入新密码）、启用/禁用、改角色、强制下线、删除
- 顶部「新建账号」按钮
- 删除 / 禁用超管时给出确认弹窗，并说明「不能删除最后一个超管」

### 5.5 顶栏

- `⋯` 菜单新增「退出登录」
- 菜单顶部显示当前账号（`display_name || username` + 角色标签）

---

## 6. 首次启动与数据迁移

```
serve() 启动
  → 确保 web_data 目录存在
  → CREATE TABLE IF NOT EXISTS ...
  → SELECT COUNT(*) FROM users
      = 0 → 创建 admin / admin123，role=superadmin，must_change_password=1
      > 0 → 什么都不做
  → 删除过期会话
  → 清理超过 24h 的 login_failures
```

- **幂等**：只在表为空时创建，重启不会重置密码
- 首次登录强制改密，改完才放行
- 忘记密码救援：`python -m core.main --reset-password admin`（终端交互输入新密码），
  不通过 API 提供，避免变成后门

---

## 7. 与 Electron / 局域网方案的衔接

原方案 `B4` 的 token 鉴权继续保留，职责是「挡住本机其他进程与 DNS rebinding」；
登录负责「认出是谁」。落地顺序建议：

1. 先做登录（本次），后端仍是浏览器形态，默认监听 `0.0.0.0`
2. 再做 Electron + token（原方案阶段 1–3）
3. 最后把两者接起来：Electron 注入 `?k=` → 用户看到登录页 → 输密码进入

注意点：

- 手机扫码 → 先过 token → 再登录一次，**不能**把 token 当作登录凭据
- `lan_enabled=true` 时强制开启 token（原方案风险表已定），再叠加登录
- 桌面端可以把 `lan_enabled` 与登录提示放在同一个「网络」分组里，但状态互不依赖

---

## 8. 测试计划

### 8.1 单元测试（新增 `tests/test_auth.py`）

- 建库幂等：连续 init 两次，用户数仍为 1
- 密码哈希：同一密码两次哈希不同（salt 随机），校验通过
- 登录：正确密码签发会话；错误密码返回统一错误
- 限流：5 次失败后第 6 次即使密码正确也被拒
- 会话：过期会话校验失败；登出后失效；改密后其他会话失效
- 权限：普通用户访问 `/api/users` → 403
- 保护规则：删除自己 / 降级最后一个超管 → 400
- 强制改密：`must_change_password=1` 时业务 API 返回 403

### 8.2 现有测试兼容

`tests/test_webui.py`、`test_http_e2e.py`、`test_workbench.py` 全部直接打 `/api/*`，
加登录后会集体 401。两种处理：

- **推荐**：测试启动服务时带 `--no-login`（守卫整体跳过）
- 备选：测试 fixture 调 `/api/auth/login` 拿 Cookie（更真实，但每个用例多一步）

### 8.3 手工验收

- 首次启动 → 登录 `admin/admin123` → 强制改密 → 进入主界面
- 超管新建账号 → 退出 → 新账号登录 → 能看到主界面但看不到「账号管理」
- 新账号首次登录被要求改密
- 连续输错 5 次 → 提示稍后再试
- 服务重启后会话仍有效（DB 持久化），7 天后失效
- 浏览器与手机（局域网）各登录一次，互不影响

---

## 9. 分期实施

| 阶段 | 内容 | 验收 |
|---|---|---|
| 1 | `auth.py` + 建表 + admin 初始化 + 登录/登出/me + 路由守卫 | 未登录访问任意 `/api/*` 返回 401；登录后正常 |
| 2 | 登录页 + AuthGate + 401 拦截 + 顶栏退出 | 刷新保持登录；退出回到登录页 |
| 3 | 超管账号管理（增删改、重置密码、强制下线） | 普通账号看不到页签，越权请求 403 |
| 4 | 强制改密 + 限流 + 会话清理 + 审计日志 | 首次登录必须改密；失败锁生效 |
| 5 | 与 Electron token 串联、README 更新 | 扫码 → token → 登录全链路通 |

---

## 10. 风险与注意点

| 风险 | 影响 | 应对 |
|---|---|---|
| SQLite 多线程写冲突 | 偶发 `database is locked` | 线程本地连接 + WAL + `busy_timeout=5000`；写操作用短事务 |
| 局域网是明文 HTTP | 密码在局域网内可被嗅探 | 文档明确说明；局域网开关处提示「仅在可信网络使用」；后续可选 HTTPS |
| 忘记 admin 密码 | 完全进不去 | 提供 `--reset-password`（仅终端可用），不开放 API |
| 加登录后旧测试全红 | CI 阻塞 | `--no-login` 开关；新增 `test_auth.py` 覆盖真实登录路径 |
| 登录页与 SSE 冲突 | 任务跑到一半被登出 | 401 只切 UI，不主动中断正在运行的 SSE；重新登录后用 `/api/history` 恢复 |
| 未来做数据隔离 | 需要改表 | 用户表不依赖隔离设计；给业务表加 `owner_id` 即可，`sessions` 表无需变 |
| 多标签页同时登录不同账号 | 同一浏览器共享 Cookie | 后者覆盖前者（符合预期），文档说明；不做多账号同浏览器并存 |

---

## 11. 需要你确认

1. **数据隔离**：先做 A（只登录、数据共享），还是直接做 B（按账号隔离）？
2. **默认密码**：`admin/admin123` + 强制改密，是否可以？
3. **角色**：只保留 `superadmin` / `user` 两档，还是需要「普通管理员」这第三档？
4. **注册方式**：只允许超管建号（本方案），还是要开放自助注册？（本地工具建议不开放）
