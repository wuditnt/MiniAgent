"""自动化任务调度线程。

设计（对应方案 §5.5 / §5.6）：
  · daemon 线程每 10s 扫一次到期任务；启动时先做崩溃恢复与「错过排期」处理
  · 全局并发上限 2，抢不到槽位就等下一轮 tick，不排队堆积
  · 真正执行交给 WebServer.run_automation_task（复用会话运行链路）
  · 调度线程与 HTTP 线程只通过数据库交互，不共享可变内存

「立即执行」由 HTTP 路由调用 run_now：只插一条 running 记录，不动排期；
并发槽位满时在后台线程里等待，不阻塞请求。
"""

from __future__ import annotations

import threading
import time

from core import automation, logging_setup

_log = logging_setup.get_logger("app")

#: 扫描周期（秒）
TICK_SECONDS = 10.0
#: 同时执行的自动化任务上限
MAX_CONCURRENT = 2


class AutomationScheduler:
    def __init__(self, server, tick: float = TICK_SECONDS, limit: int = MAX_CONCURRENT) -> None:
        self.server = server
        self.tick = float(tick)
        self._sem = threading.BoundedSemaphore(max(1, int(limit)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    # ---------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            n = automation.recover_stale_runs()
            if n:
                _log.info("automation.recover runs=%d", n)
            m = automation.advance_missed()
            if m:
                _log.info("automation.missed tasks=%d", m)
        except Exception:  # noqa: BLE001 —— 启动清理失败不能挡住调度线程
            _log.exception("automation.startup_failed")
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="automation-scheduler"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)

    # ---------------------------------------------------------- 调度

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 —— 单次 tick 失败不能让线程退出
                _log.exception("automation.tick_failed")

    def _tick(self, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        started = 0
        for task in automation.due_tasks(ts):
            if self._stop.is_set():
                break
            if not self._sem.acquire(blocking=False):
                break           # 并发已满：这一批留给下一轮，不排队
            try:
                claimed = automation.claim_scheduled(task["id"], ts)
            except Exception:  # noqa: BLE001
                self._sem.release()
                _log.exception("automation.claim_failed task=%s", task["id"])
                continue
            if claimed is None:
                self._sem.release()     # 已被别的线程领走 / 已停用
                continue
            run_id, fresh = claimed
            threading.Thread(
                target=self._execute,
                args=(fresh, run_id),
                kwargs={"slot_held": True},
                daemon=True,
                name=f"automation-run-{task['id']}",
            ).start()
            started += 1
        return started

    def run_now(self, task_id: str) -> tuple[int, dict]:
        """立即执行：返回 (run_id, task)。任务正在跑时抛 AutomationError。"""
        run_id = automation.claim_manual(task_id)
        task = automation.get_task(task_id)
        if task is None:
            raise automation.AutomationError("任务不存在")
        # 起线程前先把会话落盘：前端收到响应即可刷新出该会话
        try:
            task = {**task, "session_id": self.server.ensure_automation_session(task)}
        except Exception:  # noqa: BLE001 —— 建会话失败不阻断执行，交给执行线程兜底
            _log.exception("automation.prepare_session_failed task=%s", task_id)
        threading.Thread(
            target=self._execute,
            args=(task, run_id),
            kwargs={"wait_slot": True},
            daemon=True,
            name=f"automation-manual-{task_id}",
        ).start()
        return run_id, task

    # ---------------------------------------------------------- 执行

    def _execute(
        self,
        task: dict,
        run_id: int,
        slot_held: bool = False,
        wait_slot: bool = False,
    ) -> None:
        """起一次自动化运行并等待它结束，然后收口数据库与未读标记。"""
        got_slot = slot_held
        session = None
        outcome = {"status": "failed", "error": ""}
        try:
            if not got_slot and wait_slot:
                while not self._stop.is_set():
                    if self._sem.acquire(timeout=1.0):
                        got_slot = True
                        break
                if not got_slot:
                    automation.finish_run(run_id, "aborted", "服务正在关闭")
                    return

            done = threading.Event()

            def on_done(status: str, error: str) -> None:
                outcome["status"] = status or "failed"
                outcome["error"] = error or ""
                done.set()

            try:
                session = self.server.run_automation_task(task, run_id, on_done=on_done)
            except Exception as e:  # noqa: BLE001 —— 准备阶段失败也要落库
                status = "skipped" if int(getattr(e, "status", 0) or 0) == 409 else "failed"
                message = str(getattr(e, "message", "") or e)
                _log.warning("automation.start_failed task=%s status=%s error=%s",
                             task.get("id"), status, message)
                automation.finish_run(run_id, status, message)
                return

            while not done.wait(1.0):
                if self._stop.is_set():
                    outcome = {"status": "aborted", "error": "服务正在关闭"}
                    break

            automation.finish_run(run_id, outcome["status"], outcome["error"])
            if session is not None:
                self._mark_unread(session)
        except Exception as e:  # noqa: BLE001
            _log.exception("automation.execute_failed task=%s", task.get("id"))
            try:
                automation.finish_run(run_id, "failed", f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            if got_slot:
                try:
                    self._sem.release()
                except ValueError:  # 重复释放保护
                    pass

    @staticmethod
    def _mark_unread(session) -> None:
        """自动化跑完且没人在看 → 侧栏未读红点。"""
        try:
            if session.watching:
                return
            session.unread = True
            session.save()
        except Exception:  # noqa: BLE001 —— 未读标记失败不影响主流程
            _log.warning("automation.unread.save_failed session=%s", session.id,
                         exc_info=True)
