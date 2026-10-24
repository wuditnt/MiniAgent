"""
登录与账号体系测试 —— 纯本地 SQLite，不联网、不碰真实 web_data。

覆盖：
  · 建库与默认超管（幂等）
  · scrypt 哈希：同密码两次不同（随机 salt）、校验正确
  · 登录 / 会话解析 / 登出
  · 失败限流：5 次后锁定，换 IP 不受影响
  · 账号管理保护规则：不能删自己、不能删最后一个超管、用户名大小写不敏感唯一
  · 自助改密：保留当前会话、踢掉其它会话
"""

from __future__ import annotations

import os
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path

import core.auth as auth  # noqa: E402


def _isolate() -> None:
    """把 auth.db 指到临时目录，并丢掉线程本地连接缓存。"""
    root = tempfile.mkdtemp(prefix="miniagent-auth-test-")
    auth._DB_PATH = os.path.join(root, "auth.db")
    auth._local.__dict__.clear()
    auth.init_db()


def _expect(code: str, fn) -> None:
    try:
        fn()
    except auth.AuthError as e:
        assert e.code == code, f"期望 {code}，实际 {e.code}：{e.message}"
        return
    raise AssertionError(f"期望抛出 {code}，但调用成功了")


def test_init_and_default_admin() -> None:
    _isolate()
    assert auth.ensure_default_admin() is True
    users = auth.list_users()
    assert len(users) == 1
    admin = users[0]
    assert admin["username"] == auth.DEFAULT_ADMIN
    assert admin["role"] == auth.ROLE_SUPERADMIN
    assert admin["must_change_password"] is True, "默认账号必须强制改密"
    assert "password_hash" not in admin, "对外结构不能带哈希"

    # 幂等：再跑一次不新增、不重置
    assert auth.ensure_default_admin() is False
    assert len(auth.list_users()) == 1


def test_password_hash_and_verify() -> None:
    _isolate()
    a = auth.hash_password("secret123")
    b = auth.hash_password("secret123")
    assert a != b, "随机 salt 应让两次结果不同"
    assert a.startswith("scrypt$")
    assert auth.verify_password("secret123", a)
    assert not auth.verify_password("wrong-pass", a)
    assert not auth.verify_password("secret123", "not-a-hash")
    _expect("BAD_PASSWORD", lambda: auth.hash_password("short"))


def test_login_and_session() -> None:
    _isolate()
    auth.ensure_default_admin()

    token, user = auth.login("admin", "admin123", ip="127.0.0.1")
    assert user["username"] == "admin"
    resolved = auth.resolve_session(token)
    assert resolved is not None and resolved["id"] == user["id"]

    auth.delete_session(token)
    assert auth.resolve_session(token) is None, "登出后 token 应立即失效"


def test_login_rate_limit() -> None:
    _isolate()
    auth.ensure_default_admin()

    for _ in range(auth.FAIL_LIMIT):
        _expect("INVALID_CREDENTIALS", lambda: auth.login("admin", "bad-pass", ip="1.2.3.4"))

    # 第 6 次即使密码正确也被锁
    _expect("LOCKED", lambda: auth.login("admin", "admin123", ip="1.2.3.4"))
    assert auth.locked_seconds("admin", "1.2.3.4") > 0

    # 换一个 IP 不受影响（限流按 用户名 + IP 计）
    token, _ = auth.login("admin", "admin123", ip="5.6.7.8")
    assert auth.resolve_session(token) is not None


def test_unknown_user_uses_same_error() -> None:
    _isolate()
    auth.ensure_default_admin()
    _expect("INVALID_CREDENTIALS", lambda: auth.login("nobody", "whatever123", ip="9.9.9.9"))


def test_user_management_rules() -> None:
    _isolate()
    admin = auth.create_user(
        "admin", "admin123", role=auth.ROLE_SUPERADMIN, must_change_password=False
    )
    bob = auth.create_user("bob", "bob12345")
    assert bob["role"] == auth.ROLE_USER
    assert bob["must_change_password"] is True

    assert auth.update_user(bob["id"], display_name="Bob")["display_name"] == "Bob"

    # 不能删除自己
    _expect("SELF_FORBIDDEN", lambda: auth.delete_user(admin["id"], actor_id=admin["id"]))
    # 不能禁用自己
    _expect(
        "SELF_FORBIDDEN",
        lambda: auth.update_user(admin["id"], disabled=True, actor_id=admin["id"]),
    )
    # 不能删除 / 降级最后一个超管
    _expect(
        "LAST_SUPERADMIN",
        lambda: auth.update_user(admin["id"], role=auth.ROLE_USER, actor_id=bob["id"]),
    )
    # 用户名大小写不敏感唯一
    _expect("USERNAME_TAKEN", lambda: auth.create_user("ADMIN", "whatever123"))
    # 用户名格式
    _expect("BAD_USERNAME", lambda: auth.create_user("a", "whatever123"))


def test_change_password_keeps_current_session() -> None:
    _isolate()
    alice = auth.create_user("alice", "alice123", must_change_password=True)
    token_a, _ = auth.login("alice", "alice123")
    token_b, _ = auth.login("alice", "alice123")   # 模拟第二台设备

    auth.change_password(alice["id"], "alice123", "alice456", keep_token=token_a)

    assert auth.resolve_session(token_a) is not None, "当前会话应保留"
    assert auth.resolve_session(token_b) is None, "其它会话应被踢下线"
    user_after = auth.get_user(alice["id"])
    assert user_after is not None, "修改密码后用户应仍存在"
    assert user_after["must_change_password"] is False

    # 新密码可登录，旧密码不行
    token_c, _ = auth.login("alice", "alice456")
    assert auth.resolve_session(token_c) is not None
    _expect("INVALID_CREDENTIALS", lambda: auth.login("alice", "alice123"))


def test_disabled_account_cannot_login() -> None:
    _isolate()
    user = auth.create_user("carol", "carol123")
    token, _ = auth.login("carol", "carol123")
    assert auth.resolve_session(token) is not None

    auth.update_user(user["id"], disabled=True)
    _expect("DISABLED", lambda: auth.login("carol", "carol123"))
    assert auth.resolve_session(token) is None, "已禁用账号的既有会话应立即失效"


def test_last_seen_write_throttled() -> None:
    """每次请求都写 last_seen_at 会放大 SQLite 锁竞争，60 秒内必须只读不写。"""
    _isolate()
    auth.create_user("dave", "dave12345")
    token, _ = auth.login("dave", "dave12345")
    conn = auth._conn()
    key = auth._token_hash(token)

    def seen() -> str:
        row = conn.execute(
            "SELECT last_seen_at FROM sessions WHERE token_hash = ?", (key,)
        ).fetchone()
        return str(row["last_seen_at"])

    first = seen()
    auth.resolve_session(token)
    assert seen() == first, "节流窗口内的重复校验不应写 last_seen_at"

    old = auth._after(-120)
    with conn:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (old, key)
        )
    auth.resolve_session(token)
    assert seen() != old, "超过节流窗口后应刷新 last_seen_at"


if __name__ == "__main__":
    test_init_and_default_admin()
    test_password_hash_and_verify()
    test_login_and_session()
    test_login_rate_limit()
    test_unknown_user_uses_same_error()
    test_user_management_rules()
    test_change_password_keeps_current_session()
    test_disabled_account_cannot_login()
    test_last_seen_write_throttled()
    print("---")
    print("全部通过 ✅")
