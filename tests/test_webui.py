"""
Web UI 端到端验证：真实 WebServer + mock OpenAI 兼容服务，断言 SSE 事件序列。

覆盖（对应 docs/WEBUI_ANTDX_PLAN.md 验收标准的服务端部分）：
  ① 会话工具表矩阵：chat=空表 / chat+web={web_search} / agent=全量（纯注册表检查，不发网络）
  ② chat 模式：turn/delta/llm_done/done 事件齐全，全程无 tool 事件
  ③ /api/history：对话后能恢复 user/assistant 消息
  ④ agent 模式：tool_start/tool_end 按 id 配对 + done
  ⑤ 停止：工具执行中 POST /api/stop → cancelled 终态
  ⑥ 危险确认（拒绝）：confirm_request → 拒绝 → 不执行、文件保留 → done
  ⑦ 危险确认（允许）：文件真的被删除，tool_end ok
  ⑧ 并发保护：同会话第二个 /api/chat → 409
  ⑨ /api/state 与静态引导页
  ⑭ keep-alive：未读的请求体不影响同一条连接上的后续请求
  ⑮ 事件流：/api/trajectory 只读回放（llm_request/run_end）+ 老会话空数组 + SSE 实时镜像

运行：python test_webui.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request

# ---- 必须在导入 tools / agent 之前：铺 sys.path + 定工作区 ----
import bootstrap  # noqa: F401
from bootstrap import PACKAGE_ROOT as HERE

# 强制指向本文件的 mock 服务（直接赋值覆盖，避免读到真实 .env）
MOCK_PORT = 8775
WEB_PORT = 8776
os.environ["OPENAI_API_KEY"] = "mock-key-not-real"
os.environ["OPENAI_MODEL"] = "mock-gpt"
os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}/v1"

# ---------------------------------------------------------------- mock 模型
# 确定性脚本：按「消息历史里 tool 消息的条数」决定下一步。
# 每个场景用独立 session（历史不串），场景开始前改写 SCRIPT 即可。

SCRIPT: list[tuple[str, str]] = []
FINAL_TEXT = "【mock】任务完成。"
MOCK_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
CREATED = 1730000000


def next_action(messages: list[dict]) -> tuple[str | None, str]:
    n = sum(1 for m in messages if m.get("role") == "tool")
    if n < len(SCRIPT):
        name, args = SCRIPT[n]
        return name, json.dumps(args, ensure_ascii=False)
    return None, ""


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: A003
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        msgs = body.get("messages", [])
        model = body.get("model", "mock-gpt")
        name, args = next_action(msgs)

        if name is None:
            content, tool_calls, finish = FINAL_TEXT, None, "stop"
        else:
            content, tool_calls = None, [{
                "id": f"call_{n_tool_msgs(msgs) + 1}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }]
            finish = "tool_calls"

        if body.get("stream"):
            frames: list[dict] = []
            if content:
                for i in range(0, len(content), 8):
                    frames.append(_chunk(model, {"role": "assistant", "content": content[i:i + 8]}))
                frames.append(_chunk(model, {}, "stop"))
            else:
                frames.append(_chunk(model, {"role": "assistant", "tool_calls": tool_calls}, finish))
            frames.append(_chunk(model, {}, None, MOCK_USAGE))
            buf = "".join(
                "data: " + json.dumps(f, ensure_ascii=False) + "\n\n" for f in frames
            ) + "data: [DONE]\n\n"
            self._send(200, buf.encode("utf-8"), "text/event-stream")
            return

        message: dict = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._send(200, json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": CREATED,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": MOCK_USAGE,
        }).encode("utf-8"), "application/json")

    def do_GET(self):  # noqa: N802
        self._send(404, b"{}", "application/json")


def n_tool_msgs(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "tool")


def _chunk(model: str, delta: dict, finish=None, usage=None) -> dict:
    obj = {
        "id": "chatcmpl-mock", "object": "chat.completion.chunk",
        "created": CREATED, "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


# ---------------------------------------------------------------- Web 客户端

def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """普通 JSON 接口调用。返回 (status, obj)；4xx/5xx 不抛异常。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(f"http://127.0.0.1:{WEB_PORT}{path}", data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body.decode("utf-8", "replace")}


class SSEClient:
    """/api/chat 的 SSE 消费者：逐行解析 `data: {...}` 帧，支持边读边发后续请求。"""

    def __init__(self, payload: dict, timeout: float = 90) -> None:
        req = Request(f"http://127.0.0.1:{WEB_PORT}/api/chat",
                      data=json.dumps(payload).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json"})
        self.resp = urllib.request.urlopen(req, timeout=timeout)

    def events(self):
        for raw in self.resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                yield json.loads(line[5:])


def collect(payload: dict) -> list[dict]:
    """一次性收完整个事件流（终态后服务端关闭连接）。"""
    return list(SSEClient(payload).events())


# ---------------------------------------------------------------- 场景

def scenario_registry_matrix() -> None:
    """① 每会话工具表矩阵：不改 agent.py 就能实现双模式的根基。"""
    from core.registry import REGISTRY
    from core.webui import _session_registry

    assert len(_session_registry("chat", False).all()) == 0, "chat 关联网应为空表"
    chatweb = [t.name for t in _session_registry("chat", True).all()]
    assert chatweb == ["web_search"], f"chat 开联网应只有 web_search，实际 {chatweb}"
    assert len(_session_registry("agent", False).all()) == len(REGISTRY), "agent 模式应为全量表"
    print("  [1] 会话工具表矩阵（chat/chat+web/agent）通过")


def scenario_chat_and_history() -> None:
    """② chat 模式事件序列 + ③ history 恢复。"""
    global SCRIPT
    SCRIPT = []
    events = collect({"message": "你好", "mode": "chat", "web": False, "session": "t-chat"})
    # trace 帧是事件流的实时镜像（P0 新增，可选）；剥离它们后才是原 SSE 契约。
    # 首事件仍是 turn —— 界面渲染指令不受事件流影响（D7/D8）。
    frames = [e for e in events if e["type"] != "trace"]
    types = [e["type"] for e in frames]
    assert "turn" in types and frames[0]["type"] == "turn", f"首事件应为 turn：{types}"
    assert frames[0]["mode"] == "chat"
    # 实时镜像（可选能力）确实挂上了：首帧 trace 就是本轮的 run_start
    trace = [e for e in events if e["type"] == "trace"]
    assert trace and trace[0]["event"]["type"] == "run_start", f"应有实时轨迹帧：{trace[:1]}"
    assert types.count("delta") >= 1, "应有流式 delta"
    assert all(e["kind"] == "content" for e in frames if e["type"] == "delta")
    assert "tool_start" not in types and "tool_end" not in types, "chat 模式不应有工具事件"
    assert types[-1] == "done", f"终态应为 done：{types}"
    done = frames[-1]
    assert done["answer"] == FINAL_TEXT and done["sec"] >= 0
    # 每轮回答要带回「本轮用量增量」，供前端工具栏展示
    usage = done.get("usage")
    assert usage, f"done 应带 usage：{done}"
    assert usage["total_tokens"] == MOCK_USAGE["total_tokens"], f"用量应等于本轮消耗：{usage}"
    assert usage["llm_calls"] == 1 and usage["tool_calls"] == 0, f"chat 模式应只调 1 次模型：{usage}"

    status, hist = api("GET", "/api/history?session=t-chat")
    assert status == 200
    msgs = hist["messages"]
    assert msgs[0] == {"role": "user", "content": "你好"}
    # assistant 消息内联 usage/sec 脚注（刷新后恢复每轮用量），只比对正文与脚注存在性
    last = msgs[-1]
    assert last["role"] == "assistant" and last["content"] == FINAL_TEXT
    assert last.get("usage", {}).get("total_tokens") == MOCK_USAGE["total_tokens"]
    assert last.get("sec") is not None
    print(f"  [2] chat 模式事件序列通过（{len(types)} 个事件，无工具）")
    print(f"  [3] /api/history 通过（恢复 {len(msgs)} 条消息）")


def scenario_agent_tools() -> None:
    """④ agent 模式：工具轨迹按 id 配对（用非危险工具，避免触发确认）。"""
    global SCRIPT
    SCRIPT = [("list_dir", {"path": "."})]
    events = collect({"message": "看看目录", "mode": "agent", "web": False, "session": "t-agent"})
    types = [e["type"] for e in events]
    assert "tool_start" in types and "tool_end" in types, f"应有工具事件：{types}"
    start = next(e for e in events if e["type"] == "tool_start")
    end = next(e for e in events if e["type"] == "tool_end")
    assert start["name"] == "list_dir" and start["id"] == end["id"], "start/end 应按 id 配对"
    assert end["ok"] is True and end["result"]
    assert types[-1] == "done"
    assert events[-1]["usage"]["tool_calls"] == 1, f"用量里应记 1 次工具调用：{events[-1]}"
    print(f"  [4] agent 模式工具轨迹通过（{start['name']} 配对 id={start['id']}）")


def scenario_trajectory() -> None:
    """⑮ 事件流回放：/api/trajectory 只读，链路完整；老会话返回空数组不报错。"""
    status, body = api("GET", "/api/trajectory?session=t-chat")
    assert status == 200, body
    evs = body["events"]
    types = [e["type"] for e in evs]
    for want in ("run_start", "system_prompt", "user_message", "llm_request",
                 "llm_response", "run_end"):
        assert want in types, f"轨迹缺事件 {want}：{types}"
    assert body["last_seq"] == evs[-1]["seq"] == len(evs), f"seq 应从 1 连续：{body['last_seq']}"
    assert all(e.get("run_id") for e in evs), "每条事件都应带 run_id"
    # llm_request 只存摘要、不存正文（体积不变式）
    req = next(e for e in evs if e["type"] == "llm_request")
    assert req["payload"]["messages"] and "content" not in req["payload"], req["payload"]

    # agent 会话：工具调用与结果也要留痕
    _, ag = api("GET", "/api/trajectory?session=t-agent")
    atypes = [e["type"] for e in ag["events"]]
    assert "tool_call" in atypes and "tool_result" in atypes, atypes

    # P0 之前的老会话（无事件文件）→ 空数组，不报 404
    status, empty = api("GET", "/api/trajectory?session=no-such-session")
    assert status == 200 and empty["events"] == [] and empty["last_seq"] == 0, empty
    print(f"  [15] /api/trajectory 通过（{len(evs)} 条事件，链路完整；老会话空数组）")


def _approve(sid: str, ev: dict) -> None:
    """对一条 confirm_request 事件应答「允许」。"""
    assert ev["type"] == "confirm_request"
    status, body = api("POST", "/api/confirm",
                       {"session": sid, "id": ev["id"], "approve": True})
    assert status == 200 and body.get("ok") is True, f"confirm 应命中：{body}"


def scenario_stop() -> None:
    """⑤ 停止：工具执行中触发取消，最终收到 cancelled 终态。

    run_python 是危险工具，先在 confirm_request 时放行，等 tool_start（开始 sleep）
    后再 POST /api/stop。
    """
    global SCRIPT
    SCRIPT = [("run_python", {"code": "import time; time.sleep(3)"})]
    client = SSEClient({"message": "慢慢跑", "mode": "agent", "web": False, "session": "t-stop"})
    events = []
    for ev in client.events():
        events.append(ev)
        if ev["type"] == "confirm_request":
            _approve("t-stop", ev)
        elif ev["type"] == "tool_start":
            status, body = api("POST", "/api/stop", {"session": "t-stop"})
            assert status == 200 and body.get("ok") is True, f"stop 应成功：{body}"
    types = [e["type"] for e in events]
    assert "cancelled" in types, f"应有 cancelled 终态：{types}"
    assert "done" not in types, "停止后不应再有 done"
    # 注意：令牌会直接杀掉注册的子进程，工具结果可能是 [exit=1] 而非 [cancelled]
    # （控制台路径同样如此，属既有取消语义），所以这里不断言 tool_end 的 ok 值。
    ends = [e["type"] for e in events if e["type"] == "tool_end"]
    print(f"  [5] 停止任务通过（cancelled 终态，tool_end×{len(ends)}）")


def _ws_root() -> str:
    """Web UI 会话落在「默认工作区」目录里 —— 即用户数据目录，不再是仓库根。"""
    import core.workspace_config as wc

    d = wc.resolve_path(wc.DEFAULT_ID)
    os.makedirs(d, exist_ok=True)
    return d


def _make_file(name: str) -> None:
    with open(os.path.join(_ws_root(), name), "w", encoding="utf-8") as f:
        f.write("tmp")


def scenario_confirm(deny: bool) -> None:
    """⑥⑦ 危险确认：
    拒绝 → agent 不执行（不发 tool_start/tool_end），结果直接回给模型，最终 done；
    允许 → 文件真的被删除，tool_end ok。
    """
    global SCRIPT
    fname = f"_webui_confirm_{'deny' if deny else 'allow'}.txt"
    _make_file(fname)
    assert os.path.isfile(os.path.join(_ws_root(), fname))
    SCRIPT = [("delete_file", {"path": fname})]
    sid = "t-confirm-deny" if deny else "t-confirm-allow"

    client = SSEClient({"message": "删文件", "mode": "agent", "web": False, "session": sid})
    events = []
    for ev in client.events():
        events.append(ev)
        if ev["type"] == "confirm_request":
            assert ev["tool"] == "delete_file"
            answer = not deny
            status, body = api("POST", "/api/confirm",
                               {"session": sid, "id": ev["id"], "approve": answer})
            assert status == 200 and body.get("ok") is True, f"confirm 应命中：{body}"
    types = [e["type"] for e in events]
    assert "confirm_request" in types, f"应弹出确认：{types}"
    assert types[-1] == "done"
    if deny:
        assert "tool_start" not in types and "tool_end" not in types, "拒绝后不应有执行轨迹"
        assert os.path.isfile(os.path.join(_ws_root(), fname)), "拒绝后文件应还在"
        print("  [6] 危险确认-拒绝通过（不执行 + 文件保留）")
    else:
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["ok"] is True, f"允许后应执行成功：{end}"
        assert not os.path.isfile(os.path.join(_ws_root(), fname)), "允许后文件应被删除"
        print("  [7] 危险确认-允许通过（文件已删除）")


def scenario_409() -> None:
    """⑧ 同会话并发保护：先放行危险确认，等工具开跑后探测 409，再停止。"""
    global SCRIPT
    SCRIPT = [("run_python", {"code": "import time; time.sleep(3)"})]
    client = SSEClient({"message": "占住会话", "mode": "agent", "web": False, "session": "t-busy"})
    events = []
    for ev in client.events():
        events.append(ev)
        if ev["type"] == "confirm_request":
            _approve("t-busy", ev)
        elif ev["type"] == "tool_start":
            status, body = api("POST", "/api/chat",
                               {"message": "第二条", "mode": "agent", "session": "t-busy"})
            assert status == 409 and "error" in body, f"应为 409：{status} {body}"
            api("POST", "/api/stop", {"session": "t-busy"})
    types = [e["type"] for e in events]
    assert "cancelled" in types, f"收尾应拿到 cancelled：{types}"
    print("  [8] 同会话并发 409 通过")


def scenario_state_and_index() -> None:
    """⑨ state 字段与静态托管兜底页。"""
    status, st = api("GET", "/api/state")
    assert status == 200
    for key in ("model", "workspace", "tools", "search_backend", "confirm_timeout", "sessions"):
        assert key in st, f"state 缺字段：{key}"
    assert st["model"] == "mock-gpt" and st["tools"] > 0
    assert any(s["id"] == "t-chat" for s in st["sessions"]), "历史会话应出现在 state 里"

    req = Request(f"http://127.0.0.1:{WEB_PORT}/")
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", "replace")
        assert resp.status == 200 and "html" in resp.headers.get("Content-Type", "")
    assert "mini-agent" in html, "首页应可访问（引导页或 dist 均可）"
    print(f"  [9] /api/state + 首页托管通过（tools={st['tools']}，backend={st['search_backend']}）")


def scenario_workspaces() -> None:
    """⑩ 工作区：新建 / 路径校验 / 会话归属与过滤 / 非空与默认保护 / 删除。"""
    status, body = api("GET", "/api/workspaces")
    assert status == 200 and len(body["workspaces"]) == 1, f"初始应只有默认工作区：{body}"
    assert body["workspaces"][0]["id"] == "default"

    # 默认工作区现在锚在用户数据目录，这里换一个已存在的目录做新工作区的路径
    sub_dir = os.path.join(HERE, "tests")
    status, ws = api("POST", "/api/workspaces", {"name": "临时区", "path": sub_dir})
    assert status == 201, f"新建工作区应 201：{status} {ws}"
    wid = ws["id"]

    # 重复路径 / 不存在的目录都应被拒
    status, err = api("POST", "/api/workspaces", {"name": "重复", "path": sub_dir})
    assert status == 400 and "error" in err, f"重复路径应 400：{status} {err}"
    status, err = api("POST", "/api/workspaces",
                      {"name": "坏路径", "path": os.path.join(HERE, "_no_such_dir_")})
    assert status == 400, f"不存在的目录应 400：{status} {err}"

    # 在新区里建会话 → 归属正确；列表可按工作区过滤
    status, sess = api("POST", "/api/sessions", {"workspace": wid})
    assert status == 200 and sess["workspace_id"] == wid, f"会话应归属新区：{sess}"
    sid = sess["id"]
    status, lst = api("GET", f"/api/sessions?workspace={wid}")
    assert status == 200
    assert [s["id"] for s in lst["sessions"]] == [sid], f"过滤应只含本区会话：{lst}"

    # /api/chat 也能指定工作区（仅对「还没开始」的会话生效）；一旦有了消息就不再改
    global SCRIPT
    SCRIPT = []
    collect({"message": "你好", "mode": "chat", "web": False,
             "session": "t-ws-bind", "workspace": wid})
    _, lst2 = api("GET", f"/api/sessions?workspace={wid}")
    assert any(s["id"] == "t-ws-bind" for s in lst2["sessions"]), \
        f"新会话应落到指定的工作区：{lst2}"
    collect({"message": "再说一句", "mode": "chat", "web": False,
             "session": "t-ws-bind", "workspace": "default"})
    _, lst3 = api("GET", f"/api/sessions?workspace={wid}")
    assert any(s["id"] == "t-ws-bind" for s in lst3["sessions"]), \
        "已开始的会话不该被改到别的工作区"

    # UI 点「+」会先建一个空会话文件（默认工作区），随后第一次发送时才带上所选工作区；
    # 这种「已存在但还没开始」的会话也必须能改绑
    status, blank = api("POST", "/api/sessions", {"workspace": "default"})
    assert status == 200 and blank["workspace_id"] == "default"
    collect({"message": "你好", "mode": "chat", "web": False,
             "session": blank["id"], "workspace": wid})
    _, lst4 = api("GET", f"/api/sessions?workspace={wid}")
    assert any(s["id"] == blank["id"] for s in lst4["sessions"]), \
        f"已建好但未开始的空会话应能改绑工作区：{lst4}"
    api("DELETE", f"/api/sessions/{blank['id']}")

    # 非空工作区不可删
    status, err = api("DELETE", f"/api/workspaces/{wid}")
    assert status == 409 and "error" in err, f"非空应 409：{status} {err}"

    # 清空后可删
    api("DELETE", "/api/sessions/t-ws-bind")
    assert api("DELETE", f"/api/sessions/{sid}")[0] == 200
    status, ok = api("DELETE", f"/api/workspaces/{wid}")
    assert status == 200 and ok.get("ok"), f"空区应可删：{status} {ok}"

    # 默认工作区不可删
    status, err = api("DELETE", "/api/workspaces/default")
    assert status == 409, f"默认工作区应受保护：{status} {err}"

    # 文件夹选择框接口（mock 弹框，不真弹原生窗）：返回路径 / 取消 / 弹框失败
    import core.webui as _webui

    orig_pick = _webui.pick_folder_dialog
    try:
        pick_dir = os.path.join(HERE, "tests")
        _webui.pick_folder_dialog = lambda: pick_dir
        status, body = api("POST", "/api/workspaces/pick")
        assert status == 200 and body["path"] == pick_dir, f"pick 应返回所选路径：{body}"
        _webui.pick_folder_dialog = lambda: None
        status, body = api("POST", "/api/workspaces/pick")
        assert status == 200 and body["path"] is None, f"取消应返回 null：{body}"

        def _boom():
            raise RuntimeError("无法打开")

        _webui.pick_folder_dialog = _boom
        status, body = api("POST", "/api/workspaces/pick")
        assert status == 500 and "error" in body, f"弹框失败应 500：{status} {body}"
    finally:
        _webui.pick_folder_dialog = orig_pick

    print("  [10] 工作区：新建/归属过滤/chat 指定工作区/非空与默认保护/删除/选框接口 通过")


def scenario_models() -> None:
    """⑪ 模型配置：默认 DeepSeek / 列表脱敏 / 新增校验 / 切换 / 删除保护 / 连接测试。"""
    status, body = api("GET", "/api/models")
    assert status == 200
    assert body["active"] == "default", f"默认应选中 DeepSeek：{body['active']}"
    assert {"deepseek", "zhipu", "kimi", "qwen", "doubao", "qianfan",
            "siliconflow", "openai", "ollama", "vllm"} <= set(body["templates"]), "内置模板不全"

    first = body["providers"][0]
    assert first["model"] == "mock-gpt", f"默认配置应取自环境变量：{first}"
    assert first["has_key"] is True
    assert first["api_key_masked"].startswith("****"), "Key 必须脱敏回显"
    assert "mock-key-not-real" not in json.dumps(body), "真实 Key 不该下发"

    # 本地服务（ollama）无需 Key
    status, created = api("POST", "/api/models", {"provider": "ollama", "model": "qwen2.5:7b"})
    assert status == 201, f"新增 ollama 应 201：{status} {created}"
    pid = created["id"]

    # 模型名为空 → 400；需要 Key 的服务商缺 Key → 400
    assert api("POST", "/api/models", {"provider": "ollama", "model": ""})[0] == 400
    status, err = api("POST", "/api/models", {"provider": "deepseek", "model": "deepseek-chat"})
    assert status == 400 and "API Key" in err["error"], f"缺 Key 应 400：{status} {err}"

    # 切换当前模型 → state 跟着变，且列表里 is_active 唯一
    status, _ = api("POST", f"/api/models/{pid}/activate")
    assert status == 200
    assert api("GET", "/api/state")[1]["model"] == "qwen2.5:7b", "切换后 state.model 应更新"
    _, body = api("GET", "/api/models")
    assert sum(1 for p in body["providers"] if p["is_active"]) == 1, "当前只能有一个"

    # 未保存值也能测连接（空地址 → ok False，且不抛错）
    status, res = api("POST", "/api/models/test", {"base_url": "", "api_key": ""})
    assert status == 200 and res["ok"] is False, f"空地址应 ok=false：{res}"

    # 切回默认并删除新增；最后一条不可删
    assert api("POST", "/api/models/default/activate")[0] == 200
    assert api("GET", "/api/state")[1]["model"] == "mock-gpt"
    assert api("DELETE", f"/api/models/{pid}")[0] == 200
    status, err = api("DELETE", "/api/models/default")
    assert status == 400 and "error" in err, f"至少保留一条：{status} {err}"
    print("  [11] 模型配置：默认 DeepSeek/脱敏/新增校验/切换/删除保护 通过")


def scenario_search() -> None:
    """⑫ 搜索：多词 AND + 命中下标 + 片段 + history 回退磁盘。"""
    import core.webui as _w

    now = time.time()
    _w._save_session_file("t-search-a", {
        "id": "t-search-a", "title": "数据分析任务", "workspace_id": "default",
        "messages": [
            {"role": "user", "content": "帮我分析 sales.csv 的月度趋势"},
            {"role": "assistant", "content": "已读取 sales.csv，发现 3 月下滑"},
        ], "created_at": now, "updated_at": now,
    })
    _w._save_session_file("t-search-b", {
        "id": "t-search-b", "title": "写脚本", "workspace_id": "default",
        "messages": [
            {"role": "user", "content": "写一个 python 脚本"},
            {"role": "assistant", "content": "脚本已生成，可统计 sales"},
        ], "created_at": now, "updated_at": now,
    })

    _, r = api("GET", f"/api/sessions?q={quote('sales 月度')}")
    assert [s["id"] for s in r["sessions"]] == ["t-search-a"], f"多词 AND 应只命中 A：{r}"
    assert r["sessions"][0]["hit_index"] == 0, "应给出命中消息下标"
    assert "月度" in r["sessions"][0]["snippet"], "应给出命中片段"

    _, r = api("GET", f"/api/sessions?q={quote('sales python')}")
    assert [s["id"] for s in r["sessions"]] == ["t-search-b"], f"跨消息命中也应算命中：{r}"

    _, r = api("GET", f"/api/sessions?q={quote('月度 不存在词')}")
    assert r["sessions"] == [], "有一个词不命中就该过滤掉"

    _, r = api("GET", f"/api/sessions?q={quote('写脚本')}")
    assert r["sessions"][0]["hit_index"] == -1, "仅标题命中时 hit_index 应为 -1"

    # 本进程没跑过该会话的 Agent：history 必须回退磁盘，否则刷新后老会话显示为空
    _, h = api("GET", "/api/history?session=t-search-a")
    assert len(h["messages"]) == 2, f"history 应回退磁盘：{h}"

    api("DELETE", "/api/sessions/t-search-a")
    api("DELETE", "/api/sessions/t-search-b")
    print("  [12] 搜索：多词 AND / 命中下标与片段 / history 回退磁盘 通过")


def scenario_settings() -> None:
    """⑬ 通用配置：默认值 / 校验 / 更新生效 / 恢复默认。"""
    from core import sandbox

    status, body = api("GET", "/api/settings")
    assert status == 200, body
    vals, defs = body["values"], body["defaults"]
    assert vals == defs, f"没改过时应等于内置默认：{vals} vs {defs}"
    assert vals["max_turns"] == 10 and vals["stream"] is True and vals["max_workers"] == 4

    # 越界、类型错误、未知键都必须被拦下
    for bad in ({"max_turns": 0}, {"max_turns": 999}, {"temperature": "hot"},
                {"stream": "maybe"}, {"not_a_key": 1}):
        status, err = api("PUT", "/api/settings", bad)
        assert status == 400 and "error" in err, f"{bad} 应被拒：{status} {err}"

    assert sandbox.max_output_chars() == 8000, "初始应为默认上限"

    # 正常更新：接口返回新值，且影响进程全局的项立刻同步
    status, body = api("PUT", "/api/settings",
                       {"max_turns": 40, "temperature": 0.7,
                        "stream": False, "max_output_chars": 12345})
    assert status == 200, body
    assert body["values"]["max_turns"] == 40 and body["values"]["stream"] is False
    assert sandbox.max_output_chars() == 12345, "运行时上限应同步"

    # 新构造的 Agent 应读到新配置；显式传参仍然优先
    from core.agent import Agent

    fresh = Agent(verbose=False)
    assert fresh.max_turns == 40 and fresh.temperature == 0.7 and fresh.stream is False, (
        fresh.max_turns, fresh.temperature, fresh.stream)
    assert Agent(verbose=False, max_turns=7).max_turns == 7, "显式参数应覆盖配置"

    # 恢复默认
    status, body = api("PUT", "/api/settings", {"reset": True})
    assert status == 200 and body["values"] == body["defaults"], body
    assert sandbox.max_output_chars() == 8000, "恢复默认后运行时上限也应回退"
    print("  [13] 通用配置：默认/校验/生效/恢复默认 通过")


def scenario_keepalive_body() -> None:
    """⑭ keep-alive 复用：没读的请求体不能污染下一个请求。

    回归点：前端「设为当前 / 连接测试」会带 `{}` 请求体，而处理函数只用路径参数。
    HTTP/1.1 复用连接时若不消费这段字节，后续 GET 会被解析成畸形请求（400），
    前端表现就是「点一下按钮，模型列表突然变空」。
    """
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=15)
    try:
        conn.request("POST", "/api/models/default/activate", body=b"{}",
                     headers={"Content-Type": "application/json"})
        r1 = conn.getresponse()
        first = (r1.status, json.loads(r1.read() or b"{}"))

        # 同一条连接上紧接着发 GET —— 修复前这里会收到 400 / 错位的响应
        conn.request("GET", "/api/models")
        r2 = conn.getresponse()
        second = (r2.status, json.loads(r2.read() or b"{}"))
    finally:
        conn.close()

    assert first[0] == 200, f"activate 应 200：{first}"
    assert second[0] == 200, f"同连接上的 GET 应 200：{second}"
    assert len(second[1].get("providers") or []) >= 1, f"列表不应为空：{second[1]}"
    print("  [14] keep-alive：未读请求体不污染下一个请求 通过")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    # 隔离存储：会话 / 工作区 / 模型配置都指到临时目录，
    # 既不污染真实 web_data，也让「默认 DeepSeek」确定地从环境变量生成
    tmpdir = tempfile.mkdtemp(prefix="miniagent-webui-test-")
    import core.model_config as _mc
    import core.settings as _st
    import core.workspace_config as _wc
    import core.webui as _webui

    _webui._SESSIONS_DIR = os.path.join(tmpdir, "sessions")
    # 事件流是独立模块（core/events.py 自己算路径），也必须指到临时目录，
    # 否则测试会把 *.events.jsonl 写进真实 web_data/sessions。
    import core.events as _events

    _events._SESSIONS_DIR = os.path.join(tmpdir, "sessions")
    _mc._MODELS_FILE = os.path.join(tmpdir, "models.json")
    _wc._WORKSPACES_FILE = os.path.join(tmpdir, "workspaces.json")
    _st._SETTINGS_FILE = os.path.join(tmpdir, "settings.json")

    # mock OpenAI 兼容服务
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    # 被测的 Web UI 服务
    from core.webui import WebServer

    wsrv = WebServer(port=WEB_PORT, model="mock-gpt")
    threading.Thread(target=wsrv.serve_forever, daemon=True).start()
    # 测试里若有确认没被应答，10s 就该超时拒绝并暴露问题，不必等 120s
    _webui.CONFIRM_TIMEOUT = 10
    for _ in range(50):
        try:
            if api("GET", "/api/state")[0] == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        raise RuntimeError("WebServer 未能在 5s 内就绪")

    print(f"[mock] OpenAI 服务 :{MOCK_PORT} ｜ [web] mini-agent Web UI :{WEB_PORT}")
    print("=" * 70)

    results: list[tuple[str, bool]] = []
    scenarios = [
        ("会话工具表矩阵", scenario_registry_matrix),
        ("chat 模式 + history", scenario_chat_and_history),
        ("agent 模式工具轨迹", scenario_agent_tools),
        ("事件流轨迹回放", scenario_trajectory),
        ("停止任务", scenario_stop),
        ("危险确认-拒绝", lambda: scenario_confirm(deny=True)),
        ("危险确认-允许", lambda: scenario_confirm(deny=False)),
        ("同会话并发 409", scenario_409),
        ("state + 首页", scenario_state_and_index),
        ("工作区管理", scenario_workspaces),
        ("模型配置", scenario_models),
        ("会话搜索", scenario_search),
        ("通用配置", scenario_settings),
        ("keep-alive 请求体", scenario_keepalive_body),
    ]
    for name, fn in scenarios:
        try:
            fn()
            results.append((name, True))
        except Exception as e:  # noqa: BLE001
            results.append((name, False))
            print(f"  ! {name} 失败：{type(e).__name__}: {e}")

    mock.shutdown()
    wsrv.shutdown()

    print("=" * 70)
    for name, ok in results:
        print(f"  {name:16s} {'通过' if ok else '失败'}")
    ok = all(r for _, r in results)
    print("\nWeb UI 验证:", "全部通过" if ok else "有未通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
