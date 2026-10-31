"""
长时稳定性压测（手动运行，不登记进 run_tests.py）。

真实 WebServer + 本地 mock OpenAI 服务，6 个并发会话持续跑：
  · 每轮 SSE 必须收到 done 终态，无半截流；
  · 每 12 轮抽一次「慢工具 + 中途 stop」，必须得到 cancelled 终态；
  · 每 5s 采样健康检查、进程 RSS、线程数、/api/running 残留会话；
  · 结束后统一断言：无错误、无卡死会话、单次流全部闭合、内存增长有界。

用法（默认 600s）：
    .venv\\Scripts\\python.exe tests\\stress_webui_10m.py
    $env:STRESS_SECONDS="30"; .venv\\Scripts\\python.exe tests\\stress_webui_10m.py   # 冒烟
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

MOCK_PORT = 8772
WEB_PORT = 8773
DURATION = float(os.environ.get("STRESS_SECONDS", "600"))
WORKERS = int(os.environ.get("STRESS_WORKERS", "6"))
RAMP_RATIO = 0.03          # 前 3% 时间算预热，内存基线取预热后
PACING = (0.4, 0.7)        # 每轮之间的思考间隔（秒）
CANCEL_EVERY = int(os.environ.get("STRESS_CANCEL_EVERY", "12"))
DEBUG = os.environ.get("STRESS_DEBUG") == "1"

FINAL_TEXT = "【mock】压力轮次完成。"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
CREATED = 1730000000
SLOW_TOOL_CODE = "import time; time.sleep(1.2); print('slow-ok')"


# ---------------------------------------------------------------- mock 模型
class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            # 客户端断连/复用连接时的空体，直接 400，不能让 mock 自己抛栈刷屏
            self._send(b'{"error":"empty body"}', "application/json", 400)
            return
        msgs = body.get("messages", [])
        model = body.get("model", "mock-gpt")
        user_idx = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=0)
        user_text = str(msgs[user_idx].get("content") or "")
        # 只看「本轮用户输入之后」的工具结果：同一会话多轮复用，历史里的旧工具不能算数
        n_tool = sum(1 for m in msgs[user_idx + 1:] if m.get("role") == "tool")

        if n_tool == 0:
            # 第一轮：慢取消场景走 1.2s 的 run_python，其余走 list_dir
            if "slow" in str(user_text):
                name, args = "run_python", json.dumps({"code": SLOW_TOOL_CODE})
            else:
                name, args = "list_dir", json.dumps({"path": "."})
            content, tool_calls, finish = None, [{
                "id": "call_1", "type": "function",
                "function": {"name": name, "arguments": args},
            }], "tool_calls"
        else:
            content, tool_calls, finish = FINAL_TEXT, None, "stop"

        if not body.get("stream"):
            msg = {"role": "assistant", "content": content}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            self._send(json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion", "created": CREATED,
                "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": USAGE,
            }).encode(), "application/json")
            return

        frames: list[dict] = []
        if content:
            for i in range(0, len(content), 10):
                frames.append(_chunk(model, {"role": "assistant", "content": content[i:i + 10]}))
                time.sleep(0.015)          # 拉长流时间，让并发/取消真正交错
            frames.append(_chunk(model, {}, "stop"))
        else:
            frames.append(_chunk(model, {"role": "assistant", "tool_calls": tool_calls}, finish))
        frames.append(_chunk(model, {}, None, USAGE))
        buf = "".join("data: " + json.dumps(f, ensure_ascii=False) + "\n\n" for f in frames)
        buf += "data: [DONE]\n\n"
        self._send(buf.encode(), "text/event-stream")

    def _send(self, payload: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _chunk(model: str, delta: dict, finish=None, usage=None) -> dict:
    obj = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": CREATED,
           "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return obj


# ---------------------------------------------------------------- 客户端
def _post(path: str, payload: dict, timeout: float = 30) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{WEB_PORT}{path}",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": (e.read() or b"").decode("utf-8", "replace")}


def _events(session: str, message: str, timeout: float = 60, on_event=None) -> list[dict]:
    """收完一条 /api/chat 事件流（终态后连接关闭）；on_event 用于边读边触发取消。"""
    payload = {"message": message, "mode": "agent", "web": False, "session": session}
    req = urllib.request.Request(
        f"http://127.0.0.1:{WEB_PORT}/api/chat",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    out = []
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = (e.read() or b"").decode("utf-8", "replace")
        raise RuntimeError(f"/api/chat {e.code}: {body[:400]}")
    with resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                out.append(ev)
                if on_event is not None:
                    on_event(ev)
    return out


def _running_sessions() -> set[str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/api/running", timeout=5) as r:
        return set(json.loads(r.read() or b"{}").get("sessions", []))


def _wait_running_clear(session: str, timeout: float = 10) -> float:
    """轮询 /api/running 直到会话消失；返回等待秒数（超时返回 timeout）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if session not in _running_sessions():
                return time.time() - t0
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return timeout


def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            c = PMC()
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), ctypes.sizeof(c))
            return c.WorkingSetSize / 1e6
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------- 指标
class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.runs = 0
        self.ok = 0
        self.bad_terminal = 0
        self.http_errors = 0
        self.errors: list[str] = []
        self.latencies: list[float] = []
        self.cancel_expected = 0
        self.cancel_ok = 0
        self.health_fails = 0
        self.health_lat: list[float] = []
        self.rss: list[tuple[float, float]] = []
        self.threads_peak = 0
        self.residues: list[str] = []           # 结束后 10s 仍在 /api/running 的会话
        self.running_wait = 0.0                 # 单轮等待 /api/running 清零的最长耗时
        self.stop_evt = threading.Event()

    def add(self, func, *args) -> None:
        with self.lock:
            func(*args)


def _sample(m: Metrics) -> None:
    """每 5s：健康检查 + RSS/线程 + 卡死会话巡检。"""
    import threading as th
    while not m.stop_evt.is_set():
        t0 = time.time()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{WEB_PORT}/api/health", timeout=5) as r:
                if r.status != 200:
                    m.add(lambda: setattr(m, "health_fails", m.health_fails + 1))
                m.add(m.health_lat.append, time.time() - t0)
        except Exception:  # noqa: BLE001
            m.add(lambda: setattr(m, "health_fails", m.health_fails + 1))
        rss = _rss_mb()
        if rss is not None:
            m.add(m.rss.append, (time.time() - m.started, rss))
        with m.lock:
            m.threads_peak = max(m.threads_peak, th.active_count())
        time.sleep(5)


def _worker(idx: int, m: Metrics) -> None:
    sid = f"stress-{idx}"
    it = 0
    while not m.stop_evt.is_set():
        it += 1
        cancel_case = it % CANCEL_EVERY == 0
        msg = "slow 取消压测" if cancel_case else f"压力轮次 {it}"
        if cancel_case:
            m.add(lambda: setattr(m, "cancel_expected", m.cancel_expected + 1))
        t0 = time.time()
        try:
            if cancel_case:
                flagged = {"sent": False}

                def _on_event(ev, sid=sid, flagged=flagged) -> None:
                    if DEBUG:
                        print(f"  [dbg {sid}] {ev.get('type')}", flush=True)
                    # 事件驱动取消：看到「进入执行」的信号立刻 stop，不靠固定 sleep，
                    # 避免高负载下 stop 早于任务注册而变成空操作。
                    # 危险工具会先发 confirm_request（run_python 要确认），两种事件都算。
                    if ev.get("type") in ("tool_start", "confirm_request") and not flagged["sent"]:
                        flagged["sent"] = True
                        try:
                            flagged["stop"] = _post("/api/stop", {"session": sid}, timeout=10)
                        except Exception as e:  # noqa: BLE001
                            flagged["stop_err"] = f"{type(e).__name__}: {e}"
                        if DEBUG:
                            print(f"  [dbg {sid}] stop -> {flagged.get('stop')} "
                                  f"{flagged.get('stop_err')}", flush=True)

                try:
                    evs = _events(sid, msg, timeout=45, on_event=_on_event)
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(f"取消流未闭合：{type(e).__name__}: {e}")
                types = [e.get("type") for e in evs]
                if "cancelled" in types and "done" not in types:
                    m.add(lambda: setattr(m, "cancel_ok", m.cancel_ok + 1))
                else:
                    raise AssertionError(
                        f"取消场景终态异常（tool_start={flagged['sent']} stop={flagged.get('stop')} "
                        f"err={flagged.get('stop_err')}）：{types[-4:]}")
            else:
                evs = _events(sid, msg, timeout=90)
                types = [e.get("type") for e in evs]
                if types and types[-1] == "done":
                    m.add(lambda: setattr(m, "ok", m.ok + 1))
                else:
                    m.add(lambda: setattr(m, "bad_terminal", m.bad_terminal + 1))
            m.add(m.latencies.append, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            m.add(lambda: setattr(m, "http_errors", m.http_errors + 1))
            with m.lock:
                if len(m.errors) < 8:
                    m.errors.append(f"{sid}: {type(e).__name__}: {e}")
        finally:
            # 每轮结束都确认会话从 /api/running 消失；10s 还在 = 残留
            wait = _wait_running_clear(sid)
            with m.lock:
                m.runs += 1
                m.running_wait = max(m.running_wait, wait)
                if wait >= 10 and sid not in m.residues:
                    m.residues.append(sid)
        time.sleep(random.uniform(*PACING))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    # tools → subagent → agent 的导入顺序必须先摆正，否则首个 /api/chat 会撞循环导入
    import tools  # noqa: F401
    import tempfile
    import core.model_config as mc
    import core.settings as st
    import core.workspace_config as wc
    import core.events as ev

    tmpdir = tempfile.mkdtemp(prefix="miniagent-stress-")
    mc._MODELS_FILE = os.path.join(tmpdir, "models.json")
    entry = mc.create_entry(provider="custom", name="mock",
                            base_url=f"http://127.0.0.1:{MOCK_PORT}/v1",
                            api_key="mock-key-not-real", model="mock-gpt")
    mc.set_active(entry["id"])

    from core import webui as webui_mod
    webui_mod._SESSIONS_DIR = os.path.join(tmpdir, "sessions")
    ev._SESSIONS_DIR = os.path.join(tmpdir, "sessions")
    wc._WORKSPACES_FILE = os.path.join(tmpdir, "workspaces.json")
    st._SETTINGS_FILE = os.path.join(tmpdir, "settings.json")

    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    wsrv = webui_mod.WebServer(port=WEB_PORT, model="mock-gpt", login_enabled=False)
    threading.Thread(target=wsrv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{WEB_PORT}/api/health", timeout=3) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("WebServer 未在 5s 内就绪")

    m = Metrics()
    threading.Thread(target=_sample, args=(m,), daemon=True).start()
    workers = [threading.Thread(target=_worker, args=(i, m), daemon=True) for i in range(WORKERS)]
    for t in workers:
        t.start()

    print(f"[压测] 时长 {DURATION:.0f}s / 并发 {WORKERS} / mock :{MOCK_PORT} / web :{WEB_PORT}", flush=True)
    last = time.time()
    while time.time() - m.started < DURATION:
        time.sleep(1)
        if time.time() - last >= 30:
            last = time.time()
            with m.lock:
                done = m.runs - (m.http_errors + m.bad_terminal)
                print(f"  [{time.time() - m.started:5.0f}s] 请求 {m.runs} · 成功 {done} · "
                      f"错误 {m.http_errors + m.bad_terminal} · 取消 {m.cancel_ok}/{m.cancel_expected}",
                      flush=True)
    m.stop_evt.set()
    deadline = time.time() + 30
    for t in workers:
        t.join(timeout=max(0.1, deadline - time.time()))
    time.sleep(1)

    mock.shutdown()
    wsrv.shutdown()

    # ---------------- 汇总 ----------------
    with m.lock:
        lat = sorted(m.latencies)
        rss = m.rss
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    ramp_idx = next((i for i, (t, _) in enumerate(rss) if t >= DURATION * RAMP_RATIO), 0)
    base_rss = rss[ramp_idx][1] if rss else None
    peak_rss = max((v for _, v in rss), default=None)
    end_rss = rss[-1][1] if rss else None
    growth = (peak_rss - base_rss) if (base_rss is not None and peak_rss is not None) else None

    print("=" * 64)
    print(f"请求总数 {m.runs} ｜ 成功 {m.ok} ｜ 半截流/错误终态 {m.bad_terminal} ｜ 异常 {m.http_errors}")
    print(f"取消场景 {m.cancel_ok}/{m.cancel_expected} ｜ 健康检查失败 {m.health_fails} 次 "
          f"｜ 线程峰值 {m.threads_peak} ｜ 运行态清零最长 {m.running_wait:.1f}s")
    print(f"延迟 p50 {p50 * 1000.0:.0f}ms ｜ p95 {p95 * 1000.0:.0f}ms ｜ 最大 {lat[-1] * 1000.0:.0f}ms"
          if lat else "延迟 无样本")
    if rss:
        print(f"RSS 基线 {base_rss:.1f}MB ｜ 峰值 {peak_rss:.1f}MB ｜ 结束 {end_rss:.1f}MB ｜ "
              f"增长 {growth:+.1f}MB（{len(rss)} 个采样点）")
    else:
        print("RSS 采样不可用（psutil 缺失）")
    if m.errors:
        print("异常样例：")
        for e in m.errors:
            print("  -", e)
    if m.residues:
        print("残留会话（10s 未从 /api/running 消失）：", m.residues[:10])

    problems = []
    if m.runs < max(50, DURATION / 6):
        problems.append(f"吞吐过低：{m.runs} 次")
    if m.bad_terminal:
        problems.append(f"{m.bad_terminal} 条流未闭合")
    if m.http_errors:
        problems.append(f"{m.http_errors} 次请求异常")
    if m.cancel_ok != m.cancel_expected:
        problems.append(f"取消未全部落地：{m.cancel_ok}/{m.cancel_expected}")
    if m.health_fails:
        problems.append(f"健康检查失败 {m.health_fails} 次")
    if m.residues:
        problems.append(f"/api/running 残留 {len(m.residues)} 个会话")
    if growth is not None and growth > 100:
        problems.append(f"内存增长 {growth:.1f}MB 超过 100MB")
    print("=" * 64)
    if problems:
        print("结论：未通过 ——", "；".join(problems))
        return 1
    print("结论：通过 ✅ 长时并发稳定，SSE 全闭合，取消无竞态，内存有界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
