"""
本地账号体系 —— 登录、会话、账号管理，全部落在本地 SQLite。

为什么单独一个模块？
  · webui.py 已经 2200 行，再塞进密码哈希、建表、限流会让路由层彻底读不动；
  · 这里是「纯逻辑」：不碰 HTTP、不碰 UI，测试可以直接调；
  · 标准库就够（sqlite3 + hashlib + hmac + secrets），符合项目零依赖约束。

存储：web_data/auth.db（与 settings.json 同目录，不进版本控制）
表结构：
  users           账号（角色只有 superadmin / user 两档）
  sessions        登录会话（只存 token 的 sha256，库被拷走也无法重放）
  login_failures  失败记录（用于限流，24 小时后清理）

安全要点：
  · 密码用 scrypt 加盐哈希，参数写进字符串，以后能平滑调参；
  · 比较用 hmac.compare_digest，避免时序侧信道；
  · 用户名不存在时也跑一次假哈希，防止通过响应时间枚举账号；
  · 连续失败 5 次锁 5 分钟（按 用户名 + IP 计）。

已知边界：局域网是明文 HTTP，密码在传输层可被嗅探 —— 这是「只用可信网络」的
前提，README 里有说明；本模块只负责存储与校验，不负责传输加密。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

from ._paths import ROOT

# ---------------------------------------------------------------- 常量

DB_FILE = os.path.join(ROOT, "web_data", "auth.db")

ROLE_SUPERADMIN = "superadmin"
ROLE_USER = "user"
ROLES = (ROLE_SUPERADMIN, ROLE_USER)

#: 默认超管账号（仅在 users 表为空时创建）
DEFAULT_ADMIN = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

#: 会话有效期（秒）与会话滑动续期阈值
SESSION_TTL = 7 * 24 * 3600
SESSION_RENEW_BEFORE = 24 * 3600

#: last_seen_at 写入节流：60 秒内重复请求只读不写。
#: 不节流的话，每个 API 请求都是一次 SQLite 写，白白放大锁竞争与磁盘 IO。
LAST_SEEN_INTERVAL = 60

#: 登录限流：窗口内失败次数上限 + 锁定时长
FAIL_WINDOW = 300
FAIL_LIMIT = 5
LOCK_SECONDS = 300

#: 密码与用户名的基本约束
PASSWORD_MIN = 8
PASSWORD_MAX = 128
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{3,32}$")

#: scrypt 参数（n=2^14 约 16MB 内存、几十毫秒，本地登录足够）
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

# 测试可覆盖（与 tests/test_web_search.py 改 settings._SETTINGS_FILE 同一套做法）
_DB_PATH = DB_FILE

_local = threading.local()
_dummy_lock = threading.Lock()
_dummy_hash: str | None = None


class AuthError(Exception):
    """业务错误：code 给前端判断，message 直接展示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 连接

def _conn() -> sqlite3.Connection:
    """每线程一个连接（ThreadingHTTPServer 是一请求一线程，连接不能跨线程共享）。"""
    cached = getattr(_local, "conn", None)
    cached_path = getattr(_local, "path", None)
    if cached is not None and cached_path == _DB_PATH:
        return cached
    if cached is not None:
        try:
            cached.close()
        except sqlite3.Error:
            pass
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    # 新连接顺手确保表存在：直接构造 WebServer（测试、嵌入调用）也不会踩「no such table」
    conn.executescript(_SCHEMA)
    _local.conn = conn
    _local.path = _DB_PATH
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  username             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  password_hash        TEXT    NOT NULL,
  display_name         TEXT    NOT NULL DEFAULT '',
  role                 TEXT    NOT NULL DEFAULT 'user'
                       CHECK (role IN ('superadmin', 'user')),
  disabled             INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT    NOT NULL,
  updated_at           TEXT    NOT NULL,
  last_login_at        TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash   TEXT    PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   TEXT    NOT NULL,
  expires_at   TEXT    NOT NULL,
  last_seen_at TEXT    NOT NULL,
  ip           TEXT    NOT NULL DEFAULT '',
  user_agent   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_failures (
  username  TEXT NOT NULL,
  ip        TEXT NOT NULL DEFAULT '',
  failed_at TEXT NOT NULL,
  PRIMARY KEY (username, ip, failed_at)
);
"""


def init_db() -> None:
    """建表（幂等）。启动时调一次即可。"""
    conn = _conn()
    with conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    """UTC ISO8601（带微秒，保证同一秒内的失败记录也能各占一行）。"""
    return datetime.now(timezone.utc).isoformat()


def _after(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _ts(value) -> float | None:
    """把库里存的 ISO8601 解析成时间戳；解析失败返回 None（按「需要刷新」处理）。"""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 密码

def hash_password(password: str) -> str:
    """scrypt 加盐哈希，格式 scrypt$n$r$p$salt_hex$hash_hex。"""
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise AuthError(
            "BAD_PASSWORD", f"密码长度需在 {PASSWORD_MIN}–{PASSWORD_MAX} 个字符之间"
        )
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def _fake_verify(password: str) -> None:
    """用户名不存在时也跑一次同等开销的哈希，抹平响应时间差。"""
    global _dummy_hash
    if _dummy_hash is None:
        with _dummy_lock:
            if _dummy_hash is None:
                _dummy_hash = hash_password("mini-agent-dummy-password")
    verify_password(password, _dummy_hash)


# ---------------------------------------------------------------- 用户

def _row_to_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def list_users() -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM users ORDER BY (role = 'superadmin') DESC, id"
    ).fetchall()
    return [_row_to_user(r) for r in rows]


def get_user(uid: int) -> dict | None:
    row = _conn().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_name(username: str) -> dict | None:
    row = _conn().execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    return _row_to_user(row) if row else None


def _superadmin_count() -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'superadmin' AND disabled = 0"
    ).fetchone()
    return int(row["n"])


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    role: str = ROLE_USER,
    must_change_password: bool = True,
) -> dict:
    name = (username or "").strip()
    if not USERNAME_RE.match(name):
        raise AuthError(
            "BAD_USERNAME", "用户名需为 3–32 位字母、数字、下划线、连字符或中文"
        )
    if role not in ROLES:
        raise AuthError("BAD_ROLE", f"角色只能是 {ROLES[0]} 或 {ROLES[1]}")
    pwd_hash = hash_password(password)
    now = _now()
    conn = _conn()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role, "
                "disabled, must_change_password, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (name, pwd_hash, (display_name or "").strip(), role,
                 1 if must_change_password else 0, now, now),
            )
    except sqlite3.IntegrityError:
        raise AuthError("USERNAME_TAKEN", f"用户名 {name} 已存在") from None
    user = get_user(int(cur.lastrowid))
    assert user is not None
    return user


def update_user(
    uid: int,
    *,
    actor_id: int | None = None,
    display_name: str | None = None,
    role: str | None = None,
    disabled: bool | None = None,
    password: str | None = None,
    must_change_password: bool | None = None,
    keep_token: str | None = None,
) -> dict:
    user = get_user(uid)
    if user is None:
        raise AuthError("NOT_FOUND", "账号不存在")
    if actor_id is not None and actor_id == uid:
        # 自己不能把自己禁用 / 降级 —— 否则会把自己锁在门外
        if disabled:
            raise AuthError("SELF_FORBIDDEN", "不能禁用当前登录的账号")
        if role is not None and role != user["role"]:
            raise AuthError("SELF_FORBIDDEN", "不能修改当前登录账号的角色")
    if role is not None and role not in ROLES:
        raise AuthError("BAD_ROLE", f"角色只能是 {ROLES[0]} 或 {ROLES[1]}")
    if (role is not None and role != ROLE_SUPERADMIN) or disabled:
        if user["role"] == ROLE_SUPERADMIN and _superadmin_count() <= 1:
            raise AuthError("LAST_SUPERADMIN", "必须至少保留一个启用的超级管理员")

    fields: list[str] = []
    values: list = []
    if display_name is not None:
        fields.append("display_name = ?")
        values.append(str(display_name).strip())
    if role is not None:
        fields.append("role = ?")
        values.append(role)
    if disabled is not None:
        fields.append("disabled = ?")
        values.append(1 if disabled else 0)
    if password is not None:
        fields.append("password_hash = ?")
        values.append(hash_password(password))
    if must_change_password is not None:
        fields.append("must_change_password = ?")
        values.append(1 if must_change_password else 0)
    if not fields:
        return user
    fields.append("updated_at = ?")
    values.append(_now())
    values.append(uid)
    conn = _conn()
    with conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    if password is not None:
        # 改密后踢掉该账号的其他会话；keep_token 用于「自己改自己的密码」保留当前会话
        delete_user_sessions(uid, keep_token=keep_token)
    updated = get_user(uid)
    assert updated is not None
    return updated


def delete_user(uid: int, *, actor_id: int | None = None) -> None:
    user = get_user(uid)
    if user is None:
        raise AuthError("NOT_FOUND", "账号不存在")
    if actor_id is not None and actor_id == uid:
        raise AuthError("SELF_FORBIDDEN", "不能删除当前登录的账号")
    if user["role"] == ROLE_SUPERADMIN and _superadmin_count() <= 1:
        raise AuthError("LAST_SUPERADMIN", "必须至少保留一个启用的超级管理员")
    conn = _conn()
    with conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))


def ensure_default_admin() -> bool:
    """users 表为空时创建默认超管；返回是否新建。幂等，重启不会重置密码。"""
    conn = _conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if int(row["n"]) > 0:
        return False
    create_user(
        DEFAULT_ADMIN, DEFAULT_ADMIN_PASSWORD,
        display_name="超级管理员", role=ROLE_SUPERADMIN,
        must_change_password=True,
    )
    return True


def reset_password(username: str, password: str) -> bool:
    """救援通道：终端里重置密码（不通过 HTTP 暴露）。"""
    user = get_user_by_name(username)
    if user is None:
        return False
    update_user(user["id"], password=password, must_change_password=False)
    return True


# ---------------------------------------------------------------- 会话

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user: dict, ip: str = "", user_agent: str = "") -> str:
    """签发登录会话，返回明文 token（只此一次，库里存哈希）。"""
    token = secrets.token_hex(32)
    now = _now()
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, "
            "last_seen_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_token_hash(token), user["id"], now, _after(SESSION_TTL), now,
             (ip or "")[:64], (user_agent or "")[:255]),
        )
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
    return token


def resolve_session(token: str) -> dict | None:
    """校验 token → 返回用户；过期 / 禁用 / 不存在返回 None。

    滑动续期：剩余不足 1 天时延长，避免用户用着用着被踢。
    """
    if not token:
        return None
    conn = _conn()
    row = conn.execute(
        "SELECT u.id, u.username, u.display_name, u.role, u.disabled, "
        "       u.must_change_password, u.created_at, u.last_login_at, "
        "       s.expires_at AS sess_expires, s.last_seen_at AS sess_seen "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None

    now_ts = time.time()
    expires_ts = _ts(row["sess_expires"])
    if expires_ts is not None and expires_ts <= now_ts:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        return None
    if row["disabled"]:
        return None

    # 两件事合并成一次 UPDATE：到期前 1 天内续期；last_seen 按 60 秒节流。
    sets: list[str] = []
    values: list = []
    if expires_ts is None or expires_ts <= now_ts + SESSION_RENEW_BEFORE:
        sets.append("expires_at = ?")
        values.append(_after(SESSION_TTL))
    seen_ts = _ts(row["sess_seen"])
    if seen_ts is None or now_ts - seen_ts >= LAST_SEEN_INTERVAL:
        sets.append("last_seen_at = ?")
        values.append(_now())
    if sets:
        with conn:
            conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE token_hash = ?",
                (*values, _token_hash(token)),
            )

    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def delete_session(token: str) -> None:
    if not token:
        return
    conn = _conn()
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def delete_user_sessions(user_id: int, keep_token: str | None = None) -> None:
    """删除该账号的全部会话；keep_token 指定的那条保留（用于改密不踢自己）。"""
    conn = _conn()
    with conn:
        if keep_token:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, _token_hash(keep_token)),
            )
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def cleanup_expired() -> int:
    """清理过期会话与陈旧失败记录，返回清理行数。"""
    conn = _conn()
    with conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
        removed = cur.rowcount or 0
        cur = conn.execute(
            "DELETE FROM login_failures WHERE failed_at <= ?",
            (_after(-24 * 3600),),
        )
        removed += cur.rowcount or 0
    return removed


# ---------------------------------------------------------------- 限流

def locked_seconds(username: str, ip: str = "") -> int:
    """还剩多少秒解锁；0 表示未锁定。"""
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_failures "
        "WHERE username = ? AND ip = ? AND failed_at > ?",
        ((username or "").strip(), ip or "", _after(-FAIL_WINDOW)),
    ).fetchone()
    if int(row["n"]) < FAIL_LIMIT:
        return 0
    last = conn.execute(
        "SELECT MAX(failed_at) AS t FROM login_failures WHERE username = ? AND ip = ?",
        ((username or "").strip(), ip or ""),
    ).fetchone()
    if not last or not last["t"]:
        return 0
    try:
        last_ts = datetime.fromisoformat(str(last["t"])).timestamp()
    except ValueError:
        return 0
    remaining = int(last_ts + LOCK_SECONDS - time.time())
    return max(0, remaining)


def record_failure(username: str, ip: str = "") -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO login_failures (username, ip, failed_at) "
            "VALUES (?, ?, ?)",
            ((username or "").strip(), ip or "", _now()),
        )


def clear_failures(username: str, ip: str = "") -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "DELETE FROM login_failures WHERE username = ? AND ip = ?",
            ((username or "").strip(), ip or ""),
        )


# ---------------------------------------------------------------- 登录编排

def login(username: str, password: str, ip: str = "", user_agent: str = "") -> tuple[str, dict]:
    """校验账号密码并签发会话，返回 (token, user)。失败抛 AuthError。"""
    name = (username or "").strip()
    if not name or not password:
        raise AuthError("INVALID_CREDENTIALS", "用户名或密码错误")

    wait = locked_seconds(name, ip)
    if wait > 0:
        raise AuthError(
            "LOCKED", f"失败次数过多，请 {max(1, (wait + 59) // 60)} 分钟后再试"
        )

    row = _conn().execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    if row is None:
        _fake_verify(password)
        record_failure(name, ip)
        raise AuthError("INVALID_CREDENTIALS", "用户名或密码错误")

    if not verify_password(password, row["password_hash"]):
        record_failure(name, ip)
        raise AuthError("INVALID_CREDENTIALS", "用户名或密码错误")

    if row["disabled"]:
        raise AuthError("DISABLED", "该账号已被禁用，请联系管理员")

    clear_failures(name, ip)
    user = _row_to_user(row)
    token = create_session(user, ip=ip, user_agent=user_agent)
    return token, user


def change_password(
    user_id: int, old_password: str, new_password: str, keep_token: str | None = None
) -> None:
    row = _conn().execute(
        "SELECT password_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise AuthError("NOT_FOUND", "账号不存在")
    if not verify_password(old_password, row["password_hash"]):
        raise AuthError("INVALID_CREDENTIALS", "原密码不正确")
    if old_password == new_password:
        raise AuthError("SAME_PASSWORD", "新密码不能与原密码相同")
    update_user(
        user_id, password=new_password, must_change_password=False,
        keep_token=keep_token,
    )
