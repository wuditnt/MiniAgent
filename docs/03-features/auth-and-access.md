# 登录与账号

## 用途

默认开启登录：打开 Web UI 先看到登录页，未登录时所有 `/api/*` 一律 401。账号用于区分「谁在登录」，不是数据隔离。

## 首次启动

- 自动创建超级管理员 `admin` / `admin123`，登录后**强制修改密码**；未改密前除「修改密码」外的接口返回 403。
- 改密后该账号的其它登录会话立即失效（当前会话保留）。

## 存储与安全

| 项 | 做法 |
|---|---|
| 账号库 | `~/.mini-agent/auth.db`（SQLite），不进版本控制 |
| 密码 | `hashlib.scrypt` 加盐哈希（n=2^14, r=8, p=1），参数写进记录；比较用 `hmac.compare_digest` |
| 登录会话 | 存服务端，库里只存 token 的 sha256；有效期 7 天，剩余不足 1 天自动续期；`last_seen_at` 60 秒节流写入 |
| Cookie | `ma_session`：`HttpOnly` + `SameSite=Lax` + `Path=/` |
| Host 白名单 | 只接受回环地址、本机网卡 IP、本机主机名，挡 DNS rebinding；反向代理/自定义域名用 `MINI_AGENT_ALLOWED_HOSTS=a.example.com,b.example.com` 追加 |
| 失败限流 | 连续失败 5 次锁 5 分钟（按「用户名 + IP」计）；用户名不存在时也跑一次假哈希，避免用响应时间枚举账号 |

## 角色

| 角色 | 能做什么 |
|---|---|
| `superadmin` | 全部功能 + 账号管理（新建、改显示名/角色、启用禁用、重置密码、强制下线、删除） |
| `user` | 全部功能，看不到「账号管理」入口 |

保护规则：不能删除或禁用自己；不能删除或降级最后一个启用的超管；用户名大小写不敏感唯一。

**当前不做数据隔离**：所有账号共用同一份会话、工作区、模型配置与记忆。

## 入口

- **登录页**：用户名 + 密码，记住用户名（不记密码）。
- **顶栏账号菜单**（最右侧用户图标）：当前账号与角色、账号管理（仅超管）、修改密码、退出登录。
- 会话过期或换浏览器后自动回到登录页；正在跑的 SSE 不会被打断，重新登录后用历史恢复。

## 救援与关闭

```powershell
python -m core --reset-password admin   # 终端救援，不开放 HTTP 接口
python -m core --no-login               # 关闭登录校验，仅本机调试
```

`--no-login` 下任何能访问该端口的人都能直接操作本机文件与命令，局域网部署不要使用。

## 本机专属操作

技能启停 / 编辑 / 删除、`POST /api/shutdown` 等仅本机可操作：远程访问时前端入口隐藏，后端同样 403。

## 关键代码位置

- 账号与密码：`src/core/auth.py` 的 `init_db()` / `hash_password()` / `verify_password()` / `ensure_default_admin()` / `reset_password()` / `login()` / `change_password()`
- 会话：`create_session()` / `resolve_session()` / `delete_session()` / `delete_user_sessions()` / `cleanup_expired()`
- 限流：`locked_seconds()` / `record_failure()` / `clear_failures()`
- 用户管理：`create_user()` / `update_user()` / `delete_user()`
- 请求守卫：`src/core/webui.py` 的 `_guard()` / `_require_superadmin()` / `_require_local_only()` / `_session_token()` / `_cookie()` / `_set_cookie()`
- Host 校验：`_host_of()` / `_allowed_hosts()`
- 接口：`_handle_auth_login()` / `_handle_auth_logout()` / `_handle_auth_password()` / `_handle_users_list()` / `_handle_user_create()` / `_handle_user_update()` / `_handle_user_delete()` / `_handle_user_logout_all()`
- 前端：`web/src/AuthGate.tsx`、`components/LoginPage.tsx`、`AccountSettingsModal.tsx`、`ChangePasswordModal.tsx`、`ForceChangePassword.tsx`
- 测试：`tests/test_auth.py`

## 注意事项

- 局域网走明文 HTTP，密码在链路上可被同网段嗅探，只建议可信网络使用。
- 账号不是权限边界：两档角色都不限制工具能力，数据也不隔离。
- `--reset-password` 是终端通道，不暴露 HTTP，避免变成后门。
- 已知缺口：没有按用户 / 按工作区的权限模型（见 [known-issues](../04-operations/known-issues.md)）。
