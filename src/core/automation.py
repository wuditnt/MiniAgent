"""
自动化任务的存储与规则计算（SQLite，零新增依赖）。

职责边界：
  · 只负责「任务 / 执行记录 / 任务级对话」的落库与规则计算
  · 不启动会话、不调用模型、不知道 HTTP —— 执行链路在 core.scheduler 与 webui
  · 时间统一用 epoch 秒（REAL），与项目现有 time.time() 一致；规则按本机时区解释

连接规范沿用 core.auth：每线程一个连接、WAL、busy_timeout=5000、foreign_keys=ON，
写操作统一 BEGIN IMMEDIATE —— 「判断到期 → 推进排期 → 插入 running」必须是一把锁内
的原子动作，否则调度线程和「立即执行」会重复触发同一周期。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from core._paths import data_dir

#: 数据库文件名（与 auth.db / settings.json 同目录）
DB_FILE_NAME = "automations.db"
#: 测试可覆盖（与 tests/test_auth.py 改 auth._DB_PATH 同一套做法）
_DB_PATH = os.path.join(data_dir(), DB_FILE_NAME)

#: 固定间隔的下限（分钟）
MIN_INTERVAL_MINUTES = 5
#: 单个任务最多保留的执行记录条数
RUN_KEEP = 100
#: 每个任务级对话保留的消息条数
CHAT_KEEP = 40

KINDS = ("once", "daily", "weekly", "interval")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_local = threading.local()
_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


class AutomationError(Exception):
    """业务错误：message 直接展示给用户；code 供接口层映射状态码。"""

    def __init__(self, message: str, field: str = "", code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code


# ---------------------------------------------------------------- 连接

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  name          TEXT    NOT NULL,
  prompt        TEXT    NOT NULL,
  workspace_id  TEXT    NOT NULL,
  session_id    TEXT,
  schedule_json TEXT    NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  web           INTEGER NOT NULL DEFAULT 0,
  model_id      TEXT    NOT NULL DEFAULT '',
  created_at    REAL    NOT NULL,
  updated_at    REAL    NOT NULL,
  next_run_at   REAL    NOT NULL,
  last_run_at   REAL,
  last_status   TEXT,
  last_error    TEXT    NOT NULL DEFAULT '',
  run_count     INTEGER NOT NULL DEFAULT 0,
  missed        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_due
  ON tasks(next_run_at) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS task_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  session_id  TEXT,
  started_at  REAL    NOT NULL,
  finished_at REAL,
  status      TEXT    NOT NULL,
  error       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_task
  ON task_runs(task_id, started_at DESC);

CREATE TABLE IF NOT EXISTS task_chat_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  role       TEXT    NOT NULL,
  content    TEXT    NOT NULL,
  patch_json TEXT,
  created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_task
  ON task_chat_messages(task_id, id);
"""

SCHEMA_VERSION = 1


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """同一 DB 文件只跑一次建表 DDL（线程本地连接几乎不复用，避免每次请求抢写锁）。"""
    path = _DB_PATH
    if path in _schema_ready:
        return
    with _schema_lock:
        if path in _schema_ready:
            return
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _schema_ready.add(path)


def _conn() -> sqlite3.Connection:
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
    _ensure_schema(conn)
    _local.conn = conn
    _local.path = _DB_PATH
    return conn


class _write_tx:
    """写事务（BEGIN IMMEDIATE）。"""

    def __enter__(self) -> sqlite3.Connection:
        self.conn = _conn()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.conn.rollback()
            return False
        self.conn.commit()
        return False


# ---------------------------------------------------------------- 规则

def _parse_time(raw: object, field: str = "time") -> str:
    text = str(raw or "").strip()
    if not _TIME_RE.match(text):
        raise AutomationError("时间格式应为 HH:MM（24 小时制）", field)
    return text


def normalize_schedule(raw: object) -> dict:
    """校验并规范化规则；非法时抛 AutomationError。"""
    if not isinstance(raw, dict):
        raise AutomationError("执行规则不能为空", "schedule")
    kind = str(raw.get("kind") or "").strip()
    if kind not in KINDS:
        raise AutomationError("不支持的重复方式", "schedule")
    if kind == "once":
        try:
            at = float(raw.get("at") or 0)
        except (TypeError, ValueError):
            raise AutomationError("单次执行时间不合法", "schedule") from None
        if at <= 0:
            raise AutomationError("请选择单次执行时间", "schedule")
        return {"kind": "once", "at": at}
    if kind == "daily":
        return {"kind": "daily", "time": _parse_time(raw.get("time"))}
    if kind == "weekly":
        days_raw = raw.get("weekdays")
        if not isinstance(days_raw, list) or not days_raw:
            raise AutomationError("请至少选择一个星期", "schedule")
        days: list[int] = []
        for item in days_raw:
            try:
                d = int(item)
            except (TypeError, ValueError):
                raise AutomationError("星期取值应为 1-7（1=周一）", "schedule") from None
            if d < 1 or d > 7:
                raise AutomationError("星期取值应为 1-7（1=周一）", "schedule")
            if d not in days:
                days.append(d)
        return {"kind": "weekly", "weekdays": sorted(days), "time": _parse_time(raw.get("time"))}
    try:
        every = int(raw.get("every_minutes") or 0)
    except (TypeError, ValueError):
        raise AutomationError("间隔分钟数不合法", "schedule") from None
    if every < MIN_INTERVAL_MINUTES:
        raise AutomationError(f"间隔不能小于 {MIN_INTERVAL_MINUTES} 分钟", "schedule")
    if every > 60 * 24 * 365:
        raise AutomationError("间隔不能超过一年", "schedule")
    return {"kind": "interval", "every_minutes": every}


def _local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts))


def _at_time(day: datetime, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_after(schedule: dict, base_ts: float) -> float | None:
    """严格晚于 base_ts 的下一个触发时刻；单次任务已过期返回 None。

    全部按本机时区计算（与页面标注一致）。
    """
    kind = schedule.get("kind")
    base = float(base_ts)
    if kind == "once":
        at = float(schedule.get("at") or 0)
        return at if at > base else None
    if kind == "interval":
        every = int(schedule.get("every_minutes") or MIN_INTERVAL_MINUTES)
        return base + every * 60
    base_dt = _local_dt(base)
    hhmm = str(schedule.get("time") or "00:00")
    if kind == "daily":
        cand = _at_time(base_dt, hhmm)
        if cand.timestamp() <= base:
            cand += timedelta(days=1)
        return cand.timestamp()
    if kind == "weekly":
        days = set(int(d) for d in (schedule.get("weekdays") or []))
        for offset in range(0, 8):
            day = (base_dt + timedelta(days=offset))
            if day.isoweekday() not in days:
                continue
            cand = _at_time(day, hhmm)
            if cand.timestamp() > base:
                return cand.timestamp()
        return None
    return None


def next_runs(schedule: dict, count: int = 3, base_ts: float | None = None) -> list[float]:
    """从 base 起连续算 count 个触发时刻（用于预览）。"""
    out: list[float] = []
    cur = float(base_ts if base_ts is not None else time.time())
    for _ in range(max(0, int(count))):
        nxt = next_after(schedule, cur)
        if nxt is None:
            break
        out.append(nxt)
        cur = nxt
    return out


_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def describe_schedule(schedule: dict) -> str:
    """规则的中文摘要（列表与 AI 助手上下文共用）。"""
    kind = schedule.get("kind")
    if kind == "once":
        at = float(schedule.get("at") or 0)
        return f"单次 {_fmt_ts(at)}" if at else "单次（未设置时间）"
    if kind == "daily":
        return f"每天 {schedule.get('time')}"
    if kind == "weekly":
        days = "、".join(
            _WEEKDAY_NAMES[int(d) - 1] for d in sorted(int(x) for x in schedule.get("weekdays") or [])
            if 1 <= int(d) <= 7
        )
        return f"每周{days} {schedule.get('time')}"
    if kind == "interval":
        every = int(schedule.get("every_minutes") or 0)
        if every % 60 == 0:
            return f"每 {every // 60} 小时"
        return f"每 {every} 分钟"
    return "未知规则"


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- 任务 CRUD

def _row_to_task(row: sqlite3.Row) -> dict:
    try:
        schedule = json.loads(row["schedule_json"])
    except (TypeError, ValueError):
        schedule = {"kind": "once", "at": 0}
    task = {
        "id": row["id"],
        "name": row["name"],
        "prompt": row["prompt"],
        "workspace_id": row["workspace_id"],
        "session_id": row["session_id"] or "",
        "schedule": schedule,
        "enabled": bool(row["enabled"]),
        "web": bool(row["web"]),
        "model_id": row["model_id"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "next_run_at": row["next_run_at"],
        "last_run_at": row["last_run_at"],
        "last_status": row["last_status"] or "",
        "last_error": row["last_error"] or "",
        "run_count": int(row["run_count"] or 0),
        "missed": int(row["missed"] or 0),
    }
    task["schedule_text"] = describe_schedule(schedule)
    return task


def _validate_common(name: object, prompt: object, workspace_id: object) -> tuple[str, str, str]:
    name = str(name or "").strip()
    prompt = str(prompt or "").strip()
    wid = str(workspace_id or "").strip()
    if not name:
        raise AutomationError("任务名称不能为空", "name")
    if len(name) > 60:
        raise AutomationError("任务名称不能超过 60 字", "name")
    if not prompt:
        raise AutomationError("执行内容不能为空", "prompt")
    if not wid:
        raise AutomationError("请选择工作区", "workspace_id")
    from core import workspace_config

    if workspace_config.get_workspace(wid) is None:
        raise AutomationError("工作区不存在或已被删除", "workspace_id")
    return name, prompt, wid


def _require_future(schedule: dict) -> float:
    nxt = next_after(schedule, time.time())
    if nxt is None:
        raise AutomationError("单次执行时间必须晚于当前时间", "schedule")
    return nxt


def list_tasks() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def create_task(data: dict) -> dict:
    name, prompt, wid = _validate_common(
        data.get("name"), data.get("prompt"), data.get("workspace_id")
    )
    schedule = normalize_schedule(data.get("schedule"))
    next_run_at = _require_future(schedule)
    now = time.time()
    tid = uuid.uuid4().hex[:12]
    with _write_tx() as conn:
        conn.execute(
            "INSERT INTO tasks (id, name, prompt, workspace_id, session_id, schedule_json,"
            " enabled, web, model_id, created_at, updated_at, next_run_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid, name, prompt, wid, None, json.dumps(schedule, ensure_ascii=False),
                1 if data.get("enabled", True) else 0,
                1 if data.get("web") else 0,
                str(data.get("model_id") or "")[:64],
                now, now, next_run_at,
            ),
        )
    task = get_task(tid)
    assert task is not None
    return task


def update_task(task_id: str, patch: dict) -> dict:
    task = get_task(task_id)
    if task is None:
        raise AutomationError("任务不存在")
    merged = {
        "name": patch.get("name", task["name"]),
        "prompt": patch.get("prompt", task["prompt"]),
        "workspace_id": patch.get("workspace_id", task["workspace_id"]),
        "schedule": patch.get("schedule", task["schedule"]),
        "enabled": patch.get("enabled", task["enabled"]),
        "web": patch.get("web", task["web"]),
        "model_id": patch.get("model_id", task["model_id"]),
    }
    name, prompt, wid = _validate_common(
        merged["name"], merged["prompt"], merged["workspace_id"]
    )
    # 已经产生过会话的任务不允许换工作区：会话文件归属的工作区与任务不一致后，
    # 「之前写的文件在哪」会变得无法解释（与聊天会话同一口径）。
    if wid != task["workspace_id"] and task["session_id"]:
        raise AutomationError("任务已产生会话，不能修改工作区", "workspace_id")
    schedule = normalize_schedule(merged["schedule"])
    enabled = bool(merged["enabled"])
    now = time.time()
    schedule_changed = schedule != task["schedule"]
    next_run_at = task["next_run_at"]
    if schedule_changed or enabled:
        if schedule_changed:
            next_run_at = _require_future(schedule)
        elif next_run_at <= now:
            # 重新启用 / 排期已过：推进到下一个未来时刻，不补跑
            nxt = next_after(schedule, now)
            if nxt is None:
                enabled = False
                next_run_at = 0.0
            else:
                next_run_at = nxt
    with _write_tx() as conn:
        conn.execute(
            "UPDATE tasks SET name=?, prompt=?, workspace_id=?, schedule_json=?, enabled=?,"
            " web=?, model_id=?, updated_at=?, next_run_at=? WHERE id=?",
            (
                name, prompt, wid, json.dumps(schedule, ensure_ascii=False),
                1 if enabled else 0, 1 if merged["web"] else 0,
                str(merged["model_id"] or "")[:64], now, next_run_at, task_id,
            ),
        )
    updated = get_task(task_id)
    assert updated is not None
    return updated


def set_session_id(task_id: str, sid: str) -> None:
    with _write_tx() as conn:
        conn.execute(
            "UPDATE tasks SET session_id=?, updated_at=? WHERE id=?",
            (sid or None, time.time(), task_id),
        )


def delete_task(task_id: str) -> bool:
    with _write_tx() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return cur.rowcount > 0


def due_tasks(now: float | None = None) -> list[dict]:
    ts = time.time() if now is None else float(now)
    rows = _conn().execute(
        "SELECT * FROM tasks WHERE enabled=1 AND next_run_at > 0 AND next_run_at <= ?"
        " ORDER BY next_run_at ASC",
        (ts,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


# ---------------------------------------------------------------- 执行记录

def claim_scheduled(task_id: str, now: float | None = None) -> tuple[int, dict] | None:
    """原子领取一个到期任务：校验到期 → 推进排期 → 插入 running。

    返回 (run_id, task)；任务已不在期（被别的线程领走 / 已停用）返回 None。
    """
    ts = time.time() if now is None else float(now)
    with _write_tx() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or not row["enabled"]:
            return None
        if float(row["next_run_at"]) <= 0 or float(row["next_run_at"]) > ts:
            return None
        task = _row_to_task(row)
        schedule = task["schedule"]
        nxt = next_after(schedule, ts)
        if nxt is None:
            # 单次任务：触发后即停用
            conn.execute(
                "UPDATE tasks SET enabled=0, next_run_at=0, updated_at=? WHERE id=?",
                (ts, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET next_run_at=?, updated_at=? WHERE id=?",
                (nxt, ts, task_id),
            )
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, session_id, started_at, status)"
            " VALUES (?,?,?, 'running')",
            (task_id, task["session_id"] or None, ts),
        )
        run_id = int(cur.lastrowid)
    claimed = get_task(task_id) or task
    return run_id, claimed


def claim_manual(task_id: str, now: float | None = None) -> int:
    """立即执行：只插 running，不动排期。任务已有 running 时抛 busy。"""
    ts = time.time() if now is None else float(now)
    with _write_tx() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise AutomationError("任务不存在", code="not_found")
        busy = conn.execute(
            "SELECT id FROM task_runs WHERE task_id=? AND status='running' LIMIT 1",
            (task_id,),
        ).fetchone()
        if busy is not None:
            raise AutomationError("该任务正在执行，请等待完成", code="busy")
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, session_id, started_at, status)"
            " VALUES (?,?,?, 'running')",
            (task_id, row["session_id"] or None, ts),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, error: str = "") -> None:
    """收口一次执行：写 run 终态 + 任务汇总字段 + 裁剪历史。"""
    now = time.time()
    with _write_tx() as conn:
        row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["status"] != "running":
            return
        conn.execute(
            "UPDATE task_runs SET finished_at=?, status=?, error=? WHERE id=?",
            (now, status, str(error or "")[:2000], run_id),
        )
        conn.execute(
            "UPDATE tasks SET last_run_at=?, last_status=?, last_error=?,"
            " run_count=run_count+1, updated_at=? WHERE id=?",
            (row["started_at"], status, str(error or "")[:2000], now, row["task_id"]),
        )
        conn.execute(
            "DELETE FROM task_runs WHERE task_id=? AND id NOT IN ("
            " SELECT id FROM task_runs WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT ?)",
            (row["task_id"], row["task_id"], RUN_KEEP),
        )


def running_runs() -> dict[str, int]:
    """{task_id: run_id}：当前 running 的任务（给前端展示「执行中」）。"""
    rows = _conn().execute(
        "SELECT task_id, MAX(id) AS rid FROM task_runs WHERE status='running' GROUP BY task_id"
    ).fetchall()
    return {r["task_id"]: int(r["rid"]) for r in rows}


def runs_for(task_id: str, limit: int = 5) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT ?",
        (task_id, max(1, int(limit))),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "error": r["error"] or "",
        }
        for r in rows
    ]


def recover_stale_runs() -> int:
    """启动恢复：上次进程被杀留下的 running 一律标 aborted。"""
    now = time.time()
    with _write_tx() as conn:
        rows = conn.execute(
            "SELECT id, task_id FROM task_runs WHERE status='running'"
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE task_runs SET status='aborted', finished_at=?, error='应用退出导致中断'"
            " WHERE status='running'",
            (now,),
        )
        task_ids = {r["task_id"] for r in rows}
        for tid in task_ids:
            conn.execute(
                "UPDATE tasks SET last_status='aborted', last_error='应用退出导致中断',"
                " updated_at=? WHERE id=?",
                (now, tid),
            )
    return len(rows)


def advance_missed(now: float | None = None) -> int:
    """启动时处理停机期间错过的排期（D5：全部跳过，只推进 + 累计 missed）。"""
    ts = time.time() if now is None else float(now)
    count = 0
    with _write_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE enabled=1 AND next_run_at > 0 AND next_run_at <= ?",
            (ts,),
        ).fetchall()
        for row in rows:
            task = _row_to_task(row)
            schedule = task["schedule"]
            nxt = next_after(schedule, ts)
            if nxt is None:
                conn.execute(
                    "UPDATE tasks SET enabled=0, next_run_at=0, missed=missed+1,"
                    " last_status='skipped', last_error='错过执行（应用未运行）', updated_at=?"
                    " WHERE id=?",
                    (ts, task["id"]),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET next_run_at=?, missed=missed+1, updated_at=? WHERE id=?",
                    (nxt, ts, task["id"]),
                )
            count += 1
    return count


def prune_runs(task_id: str, keep: int = RUN_KEEP) -> None:
    with _write_tx() as conn:
        conn.execute(
            "DELETE FROM task_runs WHERE task_id=? AND id NOT IN ("
            " SELECT id FROM task_runs WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT ?)",
            (task_id, task_id, max(1, int(keep))),
        )


# ---------------------------------------------------------------- 任务级对话

def list_chat(task_id: str, limit: int = CHAT_KEEP) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM task_chat_messages WHERE task_id=? ORDER BY id DESC LIMIT ?",
        (task_id, max(1, int(limit))),
    ).fetchall()
    items = [
        {
            "id": int(r["id"]),
            "role": r["role"],
            "content": r["content"],
            "patch": json.loads(r["patch_json"]) if r["patch_json"] else None,
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]
    return items


def append_chat(task_id: str, role: str, content: str, patch: dict | None = None) -> int:
    with _write_tx() as conn:
        cur = conn.execute(
            "INSERT INTO task_chat_messages (task_id, role, content, patch_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                task_id, role, str(content or ""),
                json.dumps(patch, ensure_ascii=False) if patch is not None else None,
                time.time(),
            ),
        )
        conn.execute(
            "DELETE FROM task_chat_messages WHERE task_id=? AND id NOT IN ("
            " SELECT id FROM task_chat_messages WHERE task_id=? ORDER BY id DESC LIMIT ?)",
            (task_id, task_id, CHAT_KEEP),
        )
        return int(cur.lastrowid)


def clear_chat(task_id: str) -> int:
    with _write_tx() as conn:
        cur = conn.execute("DELETE FROM task_chat_messages WHERE task_id=?", (task_id,))
        return cur.rowcount


def prune_chat(task_id: str, keep: int = CHAT_KEEP) -> None:
    with _write_tx() as conn:
        conn.execute(
            "DELETE FROM task_chat_messages WHERE task_id=? AND id NOT IN ("
            " SELECT id FROM task_chat_messages WHERE task_id=? ORDER BY id DESC LIMIT ?)",
            (task_id, task_id, max(1, int(keep))),
        )
