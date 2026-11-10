"""
自动化任务后端测试 —— 纯本地 SQLite，不联网、不烧 token。

覆盖：
  · 规则计算：单次 / 每天 / 每周 / 固定间隔的 next_after 边界（严格晚于基准）
  · 规则校验：非法时间、星期、小于 5 分钟的间隔
  · 任务 CRUD：空名称 / 非法工作区被拒，更新重算排期，删除级联
  · 原子领取：到期校验 + 排期推进 + running 落库；重复领取返回 None
  · 立即执行：忙碌拒绝、收口写回 last_status / run_count
  · 崩溃恢复与停机错过：running → aborted；过期排期推进 + missed 累计
  · 任务级对话：只保留最近 40 条、升序返回、可清空
  · 无人值守：确认/授权自动拒绝、提问返回固定文案并留下 notice
  · 会话未读：磁盘标记可读可清
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from types import SimpleNamespace

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path

import core.automation as automation  # noqa: E402
import core.webui as webui  # noqa: E402
from core.webui import WebUI, _Session  # noqa: E402


def _isolate() -> None:
    """把 automations.db 指到临时目录，并丢掉线程本地连接与建表缓存。"""
    root = tempfile.mkdtemp(prefix="miniagent-automation-test-")
    automation._DB_PATH = os.path.join(root, "automations.db")
    automation._local.__dict__.clear()
    automation._schema_ready.clear()


def _expect(message_part: str, fn) -> None:
    try:
        fn()
    except automation.AutomationError as e:
        assert message_part in e.message, f"期望包含「{message_part}」，实际：{e.message}"
        return
    raise AssertionError(f"期望抛出 AutomationError（{message_part}），但调用成功了")


def _new_task(**over) -> dict:
    data = {
        "name": "测试任务",
        "prompt": "看看今天有什么新闻",
        "workspace_id": "default",
        "schedule": {"kind": "daily", "time": "23:59"},
        "web": False,
        "model_id": "",
        "enabled": True,
    }
    data.update(over)
    return automation.create_task(data)


# ---------------------------------------------------------------- 规则

def test_next_after_basics() -> None:
    # 2026-03-02 是周一
    monday = datetime(2026, 3, 2, 8, 30).timestamp()
    daily = {"kind": "daily", "time": "09:00"}
    nxt = automation.next_after(daily, monday)
    assert datetime.fromtimestamp(nxt) == datetime(2026, 3, 2, 9, 0), "当天未到应排当天"

    later = datetime(2026, 3, 2, 10, 0).timestamp()
    nxt = automation.next_after(daily, later)
    assert datetime.fromtimestamp(nxt) == datetime(2026, 3, 3, 9, 0), "已过应顺延次日"

    weekly = {"kind": "weekly", "weekdays": [1, 3], "time": "09:00"}
    nxt = automation.next_after(weekly, monday)
    assert datetime.fromtimestamp(nxt) == datetime(2026, 3, 2, 9, 0), "周一应命中"
    nxt = automation.next_after(weekly, datetime(2026, 3, 2, 10, 0).timestamp())
    assert datetime.fromtimestamp(nxt) == datetime(2026, 3, 4, 9, 0), "周一已过应排周三"

    assert automation.next_after({"kind": "interval", "every_minutes": 15}, monday) == monday + 900

    assert automation.next_after({"kind": "once", "at": monday + 60}, monday) == monday + 60
    assert automation.next_after({"kind": "once", "at": monday - 60}, monday) is None

    runs = automation.next_runs(daily, 3, monday)
    assert len(runs) == 3 and runs == sorted(runs), "预览应为升序 3 个"


def test_schedule_validation() -> None:
    _expect("HH:MM", lambda: automation.normalize_schedule({"kind": "daily", "time": "9:00"}))
    _expect("至少选择一个星期", lambda: automation.normalize_schedule(
        {"kind": "weekly", "weekdays": [], "time": "09:00"}))
    _expect("1-7", lambda: automation.normalize_schedule(
        {"kind": "weekly", "weekdays": [0, 3], "time": "09:00"}))
    _expect("不能小于 5 分钟", lambda: automation.normalize_schedule(
        {"kind": "interval", "every_minutes": 4}))
    _expect("不支持的重复方式", lambda: automation.normalize_schedule({"kind": "cron"}))
    assert automation.normalize_schedule({"kind": "interval", "every_minutes": 5}) == {
        "kind": "interval", "every_minutes": 5}
    assert "每天 07:00" == automation.describe_schedule({"kind": "daily", "time": "07:00"})
    assert "每周周一、周三 09:00" == automation.describe_schedule(
        {"kind": "weekly", "weekdays": [3, 1], "time": "09:00"})


# ---------------------------------------------------------------- CRUD

def test_task_crud() -> None:
    _isolate()
    task = _new_task()
    assert task["id"] and task["enabled"] is True
    assert task["next_run_at"] > datetime.now().timestamp(), "创建后必须排在未来"
    assert task["schedule_text"].startswith("每天")
    assert [t["id"] for t in automation.list_tasks()] == [task["id"]]

    updated = automation.update_task(task["id"], {"name": "改名了"})
    assert updated["name"] == "改名了" and updated["prompt"] == task["prompt"]

    updated = automation.update_task(task["id"], {
        "schedule": {"kind": "interval", "every_minutes": 30}})
    delta = updated["next_run_at"] - datetime.now().timestamp()
    assert 1795 < delta <= 1805, f"间隔任务应从当前起算，实际差 {delta}"

    _expect("任务名称不能为空", lambda: automation.create_task({
        "name": " ", "prompt": "x", "workspace_id": "default",
        "schedule": {"kind": "daily", "time": "09:00"}}))
    _expect("工作区不存在", lambda: automation.create_task({
        "name": "x", "prompt": "x", "workspace_id": "no-such-workspace",
        "schedule": {"kind": "daily", "time": "09:00"}}))
    _expect("单次执行时间必须晚于当前时间", lambda: automation.create_task({
        "name": "x", "prompt": "x", "workspace_id": "default",
        "schedule": {"kind": "once", "at": 1}}))

    assert automation.delete_task(task["id"]) is True
    assert automation.get_task(task["id"]) is None
    assert automation.delete_task(task["id"]) is False


def test_update_keeps_next_run_when_unchanged() -> None:
    _isolate()
    task = _new_task(schedule={"kind": "interval", "every_minutes": 30})
    before = task["next_run_at"]
    updated = automation.update_task(task["id"], {"prompt": "换个说法"})
    assert updated["next_run_at"] == before, "规则没变时不该重算排期"


# ---------------------------------------------------------------- 领取与收口

def test_claim_scheduled_advances() -> None:
    _isolate()
    task = _new_task(schedule={"kind": "interval", "every_minutes": 5})
    due = task["next_run_at"]
    claimed = automation.claim_scheduled(task["id"], due)
    assert claimed is not None, "到期任务应能领取"
    run_id, fresh = claimed
    assert fresh["next_run_at"] == due + 300, "领取时必须推进排期"
    assert automation.claim_scheduled(task["id"], due) is None, "同一时刻不能重复领取"
    assert automation.running_runs() == {task["id"]: run_id}

    automation.finish_run(run_id, "success")
    done = automation.get_task(task["id"])
    assert done["last_status"] == "success" and done["run_count"] == 1
    assert automation.running_runs() == {}
    runs = automation.runs_for(task["id"])
    assert runs and runs[0]["status"] == "success" and runs[0]["finished_at"]


def test_claim_once_disables_task() -> None:
    _isolate()
    at = datetime.now().timestamp() + 120
    task = _new_task(schedule={"kind": "once", "at": at})
    assert automation.claim_scheduled(task["id"], at) is not None
    after = automation.get_task(task["id"])
    assert after["enabled"] is False and after["next_run_at"] == 0, "单次触发后应停用"
    assert automation.claim_scheduled(task["id"], at + 60) is None


def test_claim_manual_busy_and_finish() -> None:
    _isolate()
    task = _new_task()
    run_id = automation.claim_manual(task["id"])
    _expect("正在执行", lambda: automation.claim_manual(task["id"]))
    automation.finish_run(run_id, "failed", "模型超时")
    done = automation.get_task(task["id"])
    assert done["last_status"] == "failed" and done["last_error"] == "模型超时"

    # 已收口的 run 重复收口不重复计数
    automation.finish_run(run_id, "success")
    assert automation.get_task(task["id"])["run_count"] == 1

    _expect("任务不存在", lambda: automation.claim_manual("missing"))


def test_recover_stale_runs() -> None:
    _isolate()
    task = _new_task()
    automation.claim_manual(task["id"])
    assert automation.recover_stale_runs() == 1
    assert automation.get_task(task["id"])["last_status"] == "aborted"
    assert automation.running_runs() == {}
    assert automation.recover_stale_runs() == 0


def test_advance_missed() -> None:
    _isolate()
    daily = _new_task()
    once = _new_task(name="单次", schedule={"kind": "once", "at": datetime.now().timestamp() + 90})
    past = datetime.now().timestamp() - 600
    with automation._write_tx() as conn:
        conn.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (past, daily["id"]))
        # 单次任务：规则里的时刻也必须在过去，否则会重新排到未来
        conn.execute(
            "UPDATE tasks SET next_run_at=?, schedule_json=? WHERE id=?",
            (past, json.dumps({"kind": "once", "at": past}), once["id"]),
        )

    assert automation.advance_missed() == 2
    d = automation.get_task(daily["id"])
    assert d["missed"] == 1 and d["next_run_at"] > datetime.now().timestamp()
    o = automation.get_task(once["id"])
    assert o["missed"] == 1 and o["enabled"] is False and o["next_run_at"] == 0


def test_chat_history_prune() -> None:
    _isolate()
    task = _new_task()
    for i in range(45):
        automation.append_chat(task["id"], "user", f"m{i}")
    items = automation.list_chat(task["id"])
    assert len(items) == automation.CHAT_KEEP
    assert items[0]["content"] == "m5" and items[-1]["content"] == "m44", "应保留最近 40 条且升序"
    automation.append_chat(task["id"], "assistant", "好的", {"name": "新名字"})
    last = automation.list_chat(task["id"])[-1]
    assert last["role"] == "assistant" and last["patch"] == {"name": "新名字"}
    assert automation.clear_chat(task["id"]) == automation.CHAT_KEEP
    assert automation.list_chat(task["id"]) == []


def test_delete_cascades_runs_and_chat() -> None:
    _isolate()
    task = _new_task()
    run_id = automation.claim_manual(task["id"])
    automation.finish_run(run_id, "success")
    automation.append_chat(task["id"], "user", "改一下时间")
    automation.delete_task(task["id"])
    assert automation.runs_for(task["id"]) == []
    assert automation.list_chat(task["id"]) == []


# ---------------------------------------------------------------- 无人值守

def test_unattended_ask_short_circuits() -> None:
    session = _Session("t-unattended", None)
    session.begin_run(1)
    session.unattended = True
    ui = WebUI(session)
    tool = SimpleNamespace(name="run_shell")
    assert ui.ask_confirm(tool, {"command": "del /s /q *"}) is False
    assert ui.ask_approval("需要访问网络") is False
    answer = ui.ask_user("要继续吗？", ["是", "否"])
    assert answer == WebUI.UNATTENDED_ASK_TEXT
    notices = [a for a in session.run["actions"] if a.get("kind") == "notice"]
    assert len(notices) == 3, f"三次自动处置都应留下轨迹，实际 {len(notices)}"
    assert all("无人值守" in a.get("name", "") for a in notices)


# ---------------------------------------------------------------- 会话未读

def test_session_unread_flag() -> None:
    root = tempfile.mkdtemp(prefix="miniagent-session-test-")
    old = webui._SESSIONS_DIR
    webui._SESSIONS_DIR = root
    try:
        webui._save_session_file("t-auto-1", {
            "id": "t-auto-1", "title": "自动化", "source": "automation",
            "automation_id": "abc", "unread": True, "messages": [],
        })
        items = webui._list_session_files()
        assert items and items[0]["source"] == "automation"
        assert items[0]["automation_id"] == "abc" and items[0]["unread"] is True
        assert webui._set_session_unread("t-auto-1", False) is True
        with open(os.path.join(root, "t-auto-1.json"), encoding="utf-8") as f:
            assert json.load(f)["unread"] is False
        assert webui._set_session_unread("no-such-session", False) is False
    finally:
        webui._SESSIONS_DIR = old


def test_parse_json_object() -> None:
    assert webui._parse_json_object('{"reply": "ok", "patch": {}}') == {
        "reply": "ok", "patch": {}}
    assert webui._parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert webui._parse_json_object('前面是闲话 {"a": 1} 后面也是') == {"a": 1}
    assert webui._parse_json_object("完全不是 JSON") is None
    assert webui._parse_json_object("[1,2]") is None


if __name__ == "__main__":
    test_next_after_basics()
    test_schedule_validation()
    test_task_crud()
    test_update_keeps_next_run_when_unchanged()
    test_claim_scheduled_advances()
    test_claim_once_disables_task()
    test_claim_manual_busy_and_finish()
    test_recover_stale_runs()
    test_advance_missed()
    test_chat_history_prune()
    test_delete_cascades_runs_and_chat()
    test_unattended_ask_short_circuits()
    test_session_unread_flag()
    test_parse_json_object()
    print("自动化任务后端 14 项全部通过")
