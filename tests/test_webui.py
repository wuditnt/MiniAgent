"""
Web UI 端到端验证：真实 WebServer + mock OpenAI 兼容服务，断言 SSE 事件序列。

覆盖（对应 docs/WEBUI_ANTDX_PLAN.md 验收标准的服务端部分）：
  ① 会话工具表矩阵：chat 不联网=零工具 / chat 联网=仅 web_search / agent 全量（纯注册表检查，不发网络）
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
import shutil
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

# 模型来自 models.json（Web UI 配置）：测试里直接造一条指向本文件的 mock 服务。
# 先把 models.json 指到临时文件再写入 —— 否则会污染用户真实的模型配置。
MOCK_PORT = 8775
WEB_PORT = 8776
import core.model_config as _model_config  # noqa: E402

_MODELS_TMPDIR = tempfile.mkdtemp(prefix="miniagent-webui-models-")
_model_config._MODELS_FILE = os.path.join(_MODELS_TMPDIR, "models.json")

_mock = _model_config.create_entry(
    provider="custom",
    name="mock",
    base_url=f"http://127.0.0.1:{MOCK_PORT}/v1",
    api_key="mock-key-not-real",
    model="mock-gpt",
)
_model_config.set_active(_mock["id"])

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
    import tools  # noqa: F401  —— 确保工具已加载（测试进程可能尚未初始化）
    from core.registry import REGISTRY
    from core.webui import _CHAT_MODE_TOOLS, _session_registry

    all_names = {t.name for t in REGISTRY.all()}
    missing = {"web_search", "fetch_url"} - all_names
    assert not missing, (
        f"工具未加载：缺 {missing}；REGISTRY 共 {len(all_names)} 个：{sorted(all_names)[:12]}"
    )
    whitelist = set(_CHAT_MODE_TOOLS)
    assert whitelist <= all_names, f"快速对话白名单里有不存在的工具：{sorted(whitelist - all_names)}"

    # 关联网：不提供搜索，保留本地抓取 + 快速对话只读白名单
    chat_local = {t.name for t in _session_registry("chat").all()}
    expect_chat = {"fetch_url"} | whitelist
    assert chat_local == expect_chat, (
        f"chat 工具表异常：多 {sorted(chat_local - expect_chat)} / 少 {sorted(expect_chat - chat_local)}"
    )
    # 开联网：搜索 + 抓取 + 白名单（写操作/执行类一律不进）
    chat_cloud = {t.name for t in _session_registry("chat", "ask", True).all()}
    expect_chat_web = {"web_search", "fetch_url"} | whitelist
    assert chat_cloud == expect_chat_web, f"chat 联网工具表异常：{sorted(chat_cloud)}"
    for bad in ("write_file", "edit_file", "run_shell", "run_python", "delete_file"):
        assert bad not in chat_cloud, f"快速对话不该暴露 {bad}"

    # chat_tools 开关关闭：只剩联网能力
    from core import settings as settings_mod
    original_get = settings_mod.get
    settings_mod.get = lambda key, *args: (
        False if key == "chat_tools" else original_get(key)
    )
    try:
        chat_off = {t.name for t in _session_registry("chat", "ask", True).all()}
    finally:
        settings_mod.get = original_get
    assert chat_off == {"web_search", "fetch_url"}, f"开关关闭后应只剩联网能力：{sorted(chat_off)}"
    # agent 模式：全量（全量档不加载 search_tools —— 所有工具都已声明）；
    # 关联网时剔除 web_search，抓取仍在；ask 档不暴露 request_approval
    agent_local = {t.name for t in _session_registry("agent", profile="full").all()}
    expect_agent = all_names - {"web_search", "request_approval", "search_tools"}
    assert agent_local == expect_agent, (
        f"agent 关联网差异：多 {sorted(agent_local - expect_agent)} / "
        f"少 {sorted(expect_agent - agent_local)}"
    )
    agent_cloud = {t.name for t in _session_registry("agent", "auto", True, profile="full").all()}
    expect_full = all_names - {"search_tools"}
    assert agent_cloud == expect_full, (
        f"auto + 联网应为全量，差异：{sorted(expect_full ^ agent_cloud)}"
    )

    # 档位：standard 只略去低频分组（常用能力必须留下），full 等于全量
    from core.webui import _STANDARD_EXCLUDED_GROUPS
    std = {t.name for t in _session_registry("agent", "auto", True, profile="standard").all()}
    assert std < all_names, f"standard 应当比全量少：{len(std)} vs {len(all_names)}"
    for t in REGISTRY.all():
        if t.name in std:
            assert t.group not in _STANDARD_EXCLUDED_GROUPS, f"{t.name} 属于应裁分组：{t.group}"
    need = {"read_file", "write_file", "read_xlsx", "run_shell", "web_search", "ask_user"}
    assert need <= std, f"standard 少了常用工具：{sorted(need - std)}"
    assert "random_int" not in std and "zip_create" not in std, "standard 应裁掉低频工具"
    assert "search_tools" in std, "standard 必须带检索工具，否则低频能力对模型不可见"
    assert "delegate" in std, "标准档应提供子智能体委派工具"
    print("  [1] 会话工具表矩阵（chat±web / agent±web / 档位）通过")


def scenario_health() -> None:
    """健康检查：免登录可访问，且带桌面壳需要的最小字段。"""
    code, body = api("GET", "/api/health")
    assert code == 200, f"health 应免登录返回 200，实际 {code}"
    for key in ("ready", "version", "pid", "port", "login_enabled", "data_dir", "uptime"):
        assert key in body, f"health 缺少字段 {key}：{body}"
    assert body["ready"] is True
    print("  [0] 健康检查通过")


def scenario_host_guard() -> None:
    """Host 头白名单：本机地址放行，外部域名拒绝（防 DNS rebinding）。"""
    from core.webui import _allowed_hosts, _host_of

    assert _host_of("127.0.0.1:8000") == "127.0.0.1"
    assert _host_of("LOCALHOST:8000") == "localhost"
    assert _host_of("[::1]:8000") == "::1"
    assert _host_of("my-pc.local") == "my-pc.local"

    allowed = _allowed_hosts()
    assert "localhost" in allowed and "127.0.0.1" in allowed
    assert "evil.example.com" not in allowed
    print("  [0] Host 头白名单通过")


def scenario_display_grouping() -> None:
    """一次回复只呈现一个气泡：ReAct 多段 assistant/tool + 内部注入消息必须合并。

    `_display_items` 是历史回放 / 搜索 / keep 截断三处共用的口径，这里直接对
    原始消息序列断言，覆盖两个用户可见的缺陷：
      ① ReAct 中途递图片（借 user 角色注入）会把一次回复切成多个气泡；
      ② 老会话文件里注入消息没留下 _injected 标记，只能靠固定文案前缀兜底识别。
    """
    from core.images import ATTACH_NOTE
    from core.webui import _display_items, _is_injected

    raw = [
        {"role": "user", "content": "给我几个选项"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果 1"},
        {"role": "assistant", "content": "这是第一张图"},
        # 新会话：注入消息带 _injected 私有标记
        {"role": "user", "_injected": "attachment_images",
         "content": [{"type": "text", "text": ATTACH_NOTE + "\n像素图 1"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "结果 2"},
        # 老会话：没有标记，只剩固定文案
        {"role": "user", "content": ATTACH_NOTE + "\n像素图 2"},
        {"role": "assistant", "content": "这是第二张图",
         "usage": {"total_tokens": 5}, "sec": 0.1},
    ]

    assert _is_injected(raw[7]), "老会话无标记的注入消息要靠文案前缀识别"
    assert not _is_injected(raw[0]), "真实用户消息不能被误判为注入"

    items = _display_items(raw)
    assert [it["role"] for it in items] == ["user", "assistant"], items
    assert items[0]["content"] == "给我几个选项"
    # 两次图片迭代被合并进同一个气泡，两段正文都保留
    assert items[1]["content"] == "这是第一张图\n\n这是第二张图", items[1]["content"]
    assert items[1]["_end"] == 8, items[1]      # keep 截断映射回原始序列的依据
    print(f"  [1b] 展示分组通过（{len(raw)} 条消息 → {len(items)} 个气泡）")


def scenario_delete_messages() -> None:
    """多选删除：_delete_display 任意下标映射 + runs 轮次重排 + 接口兜底与实际删除。"""
    from core.webui import _delete_display, _renumber_runs

    raw = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    # 删掉中间一轮（q2 + a2）：保留 q1/a1 与 q3，不做「此条及之后」连坐
    kept, deleted = _delete_display(raw, [2, 3])
    assert [m["content"] for m in kept] == ["q1", "a1", "q3"], kept
    assert deleted == {2}, f"被删的是第 2 个用户轮次：{deleted}"

    # 空选 / 下标越界 → 拒绝
    assert _delete_display(raw, []) is None
    assert _delete_display(raw, [9]) is None

    # runs 重排：被删轮次的 run 丢弃，其余轮次整体前移
    runs = [{"turn": 1}, {"turn": 2}, {"turn": 3}]
    assert _renumber_runs(runs, {2}) == [{"turn": 1}, {"turn": 2}], _renumber_runs(runs, {2})

    # ---- 接口：真实删除两轮对话里的第一轮
    global SCRIPT
    SCRIPT = []
    sid = "t-del"
    collect({"message": "第一轮", "mode": "chat", "web": False, "session": sid})
    collect({"message": "第二轮", "mode": "chat", "web": False, "session": sid})
    status, hist = api("GET", f"/api/history?session={sid}")
    assert status == 200 and len(hist["messages"]) == 4, hist["messages"]

    # 参数兜底：空数组 / 非整数 / 下标越界 → 400
    for payload in ({"indexes": []}, {"indexes": ["a"]}, {"indexes": [99]}, {}):
        s, b = api("POST", f"/api/sessions/{sid}/delete-messages", payload)
        assert s == 400 and "error" in b, f"{payload} 应 400：{s} {b}"

    status, body = api("POST", f"/api/sessions/{sid}/delete-messages", {"indexes": [0, 1]})
    assert status == 200 and body["deleted"] == 2, body

    status, hist = api("GET", f"/api/history?session={sid}")
    assert [m["content"] for m in hist["messages"]] == ["第二轮", FINAL_TEXT], hist["messages"]
    # 第一轮被删后，第二轮的 run 轮次号应从 2 前移为 1
    assert [r["turn"] for r in hist["runs"]] == [1], hist["runs"]
    print("  [1c] 多选删除通过（任意下标 + runs 重排 + 400 兜底）")


def scenario_attachment_path() -> None:
    """附件路径解析：分隔符 / 目录穿越 / 前缀兄弟目录 / 符号链接 / size 以磁盘为准。"""
    from core.webui import ATTACH_DIR, _attachment_file, _resolve_attachments

    root = tempfile.mkdtemp(prefix="ma-attach-")
    other = tempfile.mkdtemp(prefix="ma-attach-sibling-")
    try:
        os.makedirs(os.path.join(root, ATTACH_DIR), exist_ok=True)
        inside = os.path.join(root, ATTACH_DIR, "a.txt")
        with open(inside, "w", encoding="utf-8") as f:
            f.write("hello")
        # /api/upload 返回的是正斜杠，Windows 上 os.sep 是反斜杠 —— 两种都必须认
        assert _attachment_file(root, ".attachments/a.txt") == os.path.realpath(inside)
        assert _attachment_file(root, ".attachments\\a.txt") == os.path.realpath(inside)
        # 穿越、前缀兄弟目录、绝对路径一律拒绝
        assert _attachment_file(root, ".attachments/../../etc/passwd") is None
        sibling = os.path.join(other, "x.txt")
        with open(sibling, "w", encoding="utf-8") as f:
            f.write("secret")
        assert _attachment_file(root, f"../{os.path.basename(other)}/x.txt") is None
        assert _attachment_file(root, sibling) is None
        # .attachments 里指向区外的符号链接也要拒绝
        kw = {"target_is_directory": True} if os.name == "nt" else {}
        try:
            os.symlink(other, os.path.join(root, ATTACH_DIR, "lnk"), **kw)
        except OSError:
            pass   # Windows 未开开发者模式时无法建链接，跳过这一条
        else:
            assert _attachment_file(root, ".attachments/lnk/x.txt") is None
        # size 以磁盘实际大小为准，客户端少报不能绕过读取上限
        item = _resolve_attachments(
            [{"name": "a", "path": ".attachments/a.txt", "mime": "text/plain", "size": 1}], root
        )[0]
        assert item["size"] == 5, item
        assert item.get("excerpt") == "hello", item
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)
    print("  [0] 附件路径解析通过")


def scenario_session_title() -> None:
    """标题：本地规则兜底（剥口语前缀 / 附件用文件名 / 别切在 .csv 里）+ 模型标题落地与锁定。"""
    from core.webui import WebUI, _derive_title, _tidy_title

    # 规则兜底：反复剥前缀（「麻烦你帮我」→「看一下」），且不能把 sales.csv 切碎
    assert _derive_title([{"role": "user", "content": "麻烦你帮我看一下 sales.csv 的月度趋势"}]) \
        == "sales.csv 的月度趋势"
    assert _derive_title([{"role": "user", "content": "帮我写一个 python 脚本"}]) == "写一个 python 脚本"
    # 附件消息用文件名，而不是那串「[附件：…（路径 …）]」机器文案
    assert _derive_title(
        [{"role": "user", "content": "[附件：季度报表.xlsx（路径 a/季度报表.xlsx，大小 1.2KB）]"}]
    ) == "季度报表.xlsx"
    # 内部注入消息（图片说明）不能占标题，取它后面那条真实用户消息
    from core.images import ATTACH_NOTE
    assert _derive_title([
        {"role": "user", "_injected": "attachment_images",
         "content": [{"type": "text", "text": ATTACH_NOTE}]},
        {"role": "user", "content": "帮我分析销售数据"},
    ]) == "分析销售数据"

    # 规整：去书名号、去「1.」序号、超长按停顿/空格切，不切在词中间
    assert _tidy_title("《把 Word 转 Markdown》") == "把 Word 转 Markdown"
    assert _tidy_title("1. 修复上下文泄漏") == "修复上下文泄漏"
    assert _tidy_title("请分析销售数据，再看月度趋势走向并给出结论") == "请分析销售数据"
    assert _tidy_title("Please help me refactor the authentication module now") == "Please help me"

    # 模型标题落地：只认还没命名过的会话，落地即锁定（后续轮次不再改、不重复落盘）
    class _Sess:
        def __init__(self) -> None:
            self.id, self.title, self.title_custom, self.saved = "s-title", "新对话", False, 0

        def save(self) -> None:
            self.saved += 1

    s = _Sess()
    ui = WebUI(s)
    ui.session_title("修复上下文泄漏")
    assert (s.title, s.title_custom, s.saved) == ("修复上下文泄漏", True, 1), (s.title, s.saved)
    ui.session_title("另一个标题")          # 已锁定：丢弃
    ui.session_title("")                    # 空标题：丢弃
    assert (s.title, s.saved) == ("修复上下文泄漏", 1)

    renamed = _Sess()
    renamed.title_custom = True             # 用户手动改过名 → 模型标题一律不覆盖
    WebUI(renamed).session_title("不该生效")
    assert (renamed.title, renamed.saved) == ("新对话", 0)
    print("  [1c] 会话标题：规则兜底 + 模型标题落地与锁定 通过")


def scenario_chat_and_history() -> None:
    """② chat 模式事件序列 + ③ history 恢复。"""
    global SCRIPT
    SCRIPT = []
    events = collect({"message": "你好", "mode": "chat", "web": False, "session": "t-chat"})
    # trace 帧是事件流的实时镜像（P0 新增，可选）；剥离它们后才是原 SSE 契约。
    # 首帧 run_start 带服务端起点（计时归后端），其后的 turn 仍是界面渲染指令。
    frames = [e for e in events if e["type"] != "trace"]
    types = [e["type"] for e in frames]
    assert types[:2] == ["run_start", "turn"], f"应先发 run_start 再 turn：{types}"
    assert frames[0]["started_at"] > 0 and frames[0]["actions"], f"run_start 应带起点与首个动作：{frames[0]}"
    assert frames[1]["mode"] == "chat"
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

    # 会话模式随消息落盘：侧栏「对话/任务」标签与切回会话还原模式都靠这个字段
    _s, lst = api("GET", "/api/sessions")
    item = next((x for x in lst["sessions"] if x["id"] == "t-chat"), None)
    assert item is not None and item.get("mode") == "chat", f"会话应落盘 mode=chat：{item}"


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
    # （串行路径同样如此，属既有取消语义），所以这里不断言 tool_end 的 ok 值。
    ends = [e["type"] for e in events if e["type"] == "tool_end"]
    print(f"  [5] 停止任务通过（cancelled 终态，tool_end×{len(ends)}）")


def scenario_detach_keeps_running() -> None:
    """⑯ 页面断开不取消任务：后台继续跑完、落盘，并释放会话锁。

    前后端分离：执行归后端，前端只是「看」。关页面只应停止推送，
    不能像点停止那样把任务杀掉。
    """
    global SCRIPT
    SCRIPT = [("run_python", {"code": "import time; time.sleep(1.5)"})]
    sid = "t-detach"
    client = SSEClient({"message": "慢慢跑", "mode": "agent", "web": False, "session": sid})
    for ev in client.events():
        if ev["type"] == "confirm_request":
            _approve(sid, ev)
        elif ev["type"] == "tool_start":
            break
    client.resp.close()          # 关掉页面：SSE 断开，任务应继续

    deadline = time.time() + 25
    content = ""
    while time.time() < deadline:
        _status, body = api("GET", f"/api/history?session={sid}")
        msgs = (body or {}).get("messages") or []
        last = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None)
        content = (last or {}).get("content") or ""
        if content.strip():
            break
        time.sleep(0.5)
    assert content.strip(), "断连后任务应在后台跑完（history 里应有回答）"

    # 事件流是权威事实：最后一次 run_end 必须是 done，而不是 cancelled
    from core import events as events_mod

    ends = [e for e in events_mod.iter_events(sid) if e.get("type") == events_mod.RUN_END]
    assert ends, "应有 run_end 事件"
    assert ends[-1]["payload"].get("status") == "done", f"断连不应取消任务：{ends[-1]}"

    # 工作线程收尾时释放会话锁：同一会话应能立刻继续对话（不 409）
    time.sleep(0.8)
    SCRIPT = []
    try:
        again = collect({"message": "再来一次", "mode": "agent", "web": False, "session": sid})
    except Exception as e:  # noqa: BLE001 —— 409 会在这里抛 HTTPError
        raise AssertionError(f"断连跑完后会话应可继续使用：{e}") from None
    assert any(e.get("type") == "done" for e in again), "后续消息应正常完成"
    print("  [6] 页面断开不取消任务通过（后台跑完、落盘、锁已释放）")


def scenario_running_indicator() -> None:
    """⑰ 运行中指示器：/api/running 与列表 / 历史里的 running 字段。

    侧栏要能看出「哪些对话正在执行」。这个状态必须来自后端内存，
    而不是前端自己的 busy 标记 —— 否则换个标签页或刷新页面就丢了。
    """
    global SCRIPT
    sid = "t-running"
    # 先跑完一轮，让会话在磁盘上有一行（列表接口是扫磁盘的）
    SCRIPT = []
    events = collect({"message": "先打个底", "mode": "agent", "web": False, "session": sid})
    assert any(e.get("type") == "done" for e in events), "前置短任务应完成"

    SCRIPT = [("run_python", {"code": "import time; time.sleep(2)"})]
    client = SSEClient({"message": "跑久一点", "mode": "agent", "web": False, "session": sid})
    seen = False
    for ev in client.events():
        if ev["type"] == "confirm_request":
            _approve(sid, ev)
        elif ev["type"] == "tool_start":
            status, body = api("GET", "/api/running")
            assert status == 200, body
            assert sid in body["sessions"], f"运行中的会话应出现在 /api/running：{body}"
            assert body["sessions"][sid]["since"] > 0, body
            _s, lst = api("GET", "/api/sessions")
            item = next((x for x in lst["sessions"] if x["id"] == sid), None)
            assert item is not None, "会话应在列表中"
            assert item.get("running") is True, f"/api/sessions 应标 running：{item}"
            _s, hist = api("GET", f"/api/history?session={sid}")
            assert hist.get("running") is True, f"/api/history 应标 running：{hist.keys()}"
            seen = True
    assert seen, "应在工具执行期间抓到运行状态"

    # 结束后从 /api/running 消失（轮询到端到端一致）
    deadline = time.time() + 10
    while time.time() < deadline:
        _s, body = api("GET", "/api/running")
        if sid not in body.get("sessions", {}):
            break
        time.sleep(0.5)
    else:
        raise AssertionError("/api/running 应清掉已结束的会话")
    _s, lst = api("GET", "/api/sessions")
    item = next((x for x in lst["sessions"] if x["id"] == sid), None)
    assert item and item.get("running") is False, f"结束后应标非运行：{item}"
    # 本轮执行记录随历史回放：起止时间由后端定稿，动作时间线可展开
    _s, hist = api("GET", f"/api/history?session={sid}")
    run = next((r for r in (hist.get("runs") or []) if r.get("turn") == 2), None)
    assert run, f"history 应带回本轮执行记录：{hist.get('runs')}"
    assert run["status"] == "done" and (run.get("elapsed_ms") or 0) > 0, run
    acts = run["actions"]
    assert acts and acts[0]["kind"] == "step", acts
    tool = next((a for a in acts if a["kind"] == "tool"), None)
    assert tool and tool["status"] == "ok" and (tool.get("dur_ms") or 0) >= 1000, acts
    # detail 必须是单行字符串：前端时间线直接渲染它，对象会把 React 弄白屏
    assert isinstance(tool.get("detail"), str), tool
    assert hist.get("server_now"), "history 应带服务端时间"
    print("  [7] 运行中指示器通过（/api/running、列表、历史三者一致）")


def scenario_reconnect() -> None:
    """⑱ 接入后台任务：GET /api/chat/stream 先发快照，再实时广播到终态。

    刷新页面 / 换标签页 / 断线回来时，前端靠这条通道把「正在跑的那一轮」
    重新接上：第一帧是 snapshot（已发生的消息 + running），之后照常收事件。
    """
    global SCRIPT
    sid = "t-reconnect"
    SCRIPT = [("run_python", {"code": "import time; time.sleep(3)"})]
    # 第一个连接发起任务；读到 tool_start（工具开始 sleep）就断开，模拟关页面
    first = SSEClient({"message": "慢慢跑", "mode": "agent", "web": False, "session": sid})
    for ev in first.events():
        if ev["type"] == "confirm_request":
            _approve(sid, ev)
        elif ev["type"] == "tool_start":
            break
    first.resp.close()

    # 重连：必须第一帧就是 snapshot，且 running=true、带着已经发生的用户消息
    resp = urllib.request.urlopen(
        f"http://127.0.0.1:{WEB_PORT}/api/chat/stream?session={sid}", timeout=60)
    events = []
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        events.append(json.loads(line[5:]))
    types = [e["type"] for e in events]
    assert types and types[0] == "snapshot", f"重连第一帧应是 snapshot：{types[:3]}"
    assert events[0]["running"] is True, f"重连时应仍在运行：{events[0]}"
    assert any(m.get("role") == "user" for m in events[0]["messages"]), "快照应含已发生的消息"
    assert "done" in types, f"重连后应收到终态：{types}"

    # 空闲会话再接入：只给快照，running=false，连接随即关闭
    time.sleep(1.0)   # 等 worker 的 finally 收尾（running_since 清空）
    resp2 = urllib.request.urlopen(
        f"http://127.0.0.1:{WEB_PORT}/api/chat/stream?session={sid}", timeout=30)
    idle = [json.loads(raw.decode("utf-8").strip()[5:])
            for raw in resp2 if raw.decode("utf-8").strip().startswith("data:")]
    assert len(idle) == 1 and idle[0]["type"] == "snapshot", f"空闲会话只该有快照：{idle}"
    assert idle[0]["running"] is False, f"结束后 running 应为 false：{idle[0]}"
    print("  [8] 接入后台任务通过（快照 + 实时广播到终态）")


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
            # 运行中的会话不允许被截断 / 删除消息 / 改配置（与 worker 抢写盘）
            for path, payload in (
                ("/api/sessions/t-busy/truncate", {"keep": 0}),
                ("/api/sessions/t-busy/delete-messages", {"indexes": [0]}),
                ("/api/sessions/t-busy/model", {"web": True}),
            ):
                s2, b2 = api("POST", path, payload)
                assert s2 == 409 and "error" in b2, f"{path} 运行中应为 409：{s2} {b2}"
            api("POST", "/api/stop", {"session": "t-busy"})
    types = [e["type"] for e in events]
    assert "cancelled" in types, f"收尾应拿到 cancelled：{types}"
    print("  [8] 同会话并发 409 通过")


def scenario_state_and_index() -> None:
    """⑨ state 字段与静态托管兜底页。"""
    status, st = api("GET", "/api/state")
    assert status == 200
    for key in ("model", "workspace", "tools", "search_backend", "sessions"):
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
    """⑪ 模型配置：测试预置的 mock 为当前项 / 列表脱敏 / 新增校验 / 切换 / 删除保护 / 连接测试。"""
    status, body = api("GET", "/api/models")
    assert status == 200
    assert body["active"] == _mock["id"], f"应选中测试预置的 mock 服务商：{body['active']}"
    assert {"deepseek", "zhipu", "kimi", "qwen", "doubao", "qianfan",
            "siliconflow", "openai", "ollama", "vllm"} <= set(body["templates"]), "内置模板不全"

    first = next(p for p in body["providers"] if p["id"] == _mock["id"])
    assert first["model"] == "mock-gpt", f"应能查到测试写入的 mock：{first}"
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

    # 切回测试预置的 mock；最后一条不可删
    assert api("POST", f"/api/models/{_mock['id']}/activate")[0] == 200
    assert api("GET", "/api/state")[1]["model"] == "mock-gpt"

    # ---- 会话级模型配置：每个对话单独保存，互不影响 ----
    import core.webui as _w

    _, sess = api("POST", "/api/sessions", {"workspace": "default"})
    msid = sess["id"]
    # 设置推理等级 → 落盘 + 列表可见
    status, r = api("POST", f"/api/sessions/{msid}/model", {"reasoning": "max"})
    assert status == 200 and r["reasoning"] == "max", f"设置推理等级应 200：{status} {r}"
    disk = _w._load_session_file(msid)
    assert disk.get("reasoning") == "max", f"推理等级应写进会话 JSON：{disk.get('reasoning')}"
    _, lst = api("GET", "/api/sessions")
    mine = next(s for s in lst["sessions"] if s["id"] == msid)
    assert mine["reasoning"] == "max", "会话列表应返回本会话的推理等级"
    # 换成 ollama（先创建一条可用的模型记录），model_id 也持久化
    status, created = api("POST", "/api/models", {"provider": "ollama", "model": "qwen2.5:7b"})
    pid2 = created["id"]
    assert api("POST", f"/api/sessions/{msid}/model", {"model_id": pid2})[0] == 200
    assert _w._load_session_file(msid).get("model_id") == pid2, "model_id 应写进会话 JSON"
    # 缺省项保持原值：只改 model_id 不动 reasoning
    assert _w._load_session_file(msid).get("reasoning") == "max"
    # 非法值：不存在的模型 / 非法等级（含已废弃的 off）→ 400，且不落盘
    assert api("POST", f"/api/sessions/{msid}/model", {"model_id": "no-such"})[0] == 400
    assert api("POST", f"/api/sessions/{msid}/model", {"reasoning": "bogus"})[0] == 400
    assert api("POST", f"/api/sessions/{msid}/model", {"reasoning": "off"})[0] == 400
    assert api("POST", "/api/sessions/no-such-session/model", {"reasoning": "low"})[0] == 404
    assert _w._load_session_file(msid).get("model_id") == pid2
    # 清理
    api("DELETE", f"/api/sessions/{msid}")
    assert api("DELETE", f"/api/models/{pid2}")[0] == 200
    assert api("DELETE", f"/api/models/{pid}")[0] == 200
    # 删除保护：删到只剩一条时不允许再删（先删掉内置 default，留下 mock 作为最后一条）
    assert api("DELETE", "/api/models/default")[0] == 200
    status, err = api("DELETE", f"/api/models/{_mock['id']}")
    assert status == 400 and "error" in err, f"至少保留一条：{status} {err}"
    print("  [11] 模型配置：默认/脱敏/新增校验/切换/删除保护 + 会话级模型配置 通过")


def scenario_session_model_restore(wsrv) -> None:
    """重启后会话模型仍生效：元数据必须在构造 Agent 之前恢复。

    回归点：Agent 的 model / base_url 是构造时定型的。如果先建 Agent 再从磁盘
    补 model_id，重启后的会话会悄悄退回全局默认模型（界面上选好的模型没了）。
    """
    import core.webui as _w

    _, created = api("POST", "/api/models", {"provider": "ollama", "model": "qwen2.5:7b"})
    pid = created["id"]
    now = time.time()
    sid = "t-restore"
    _w._save_session_file(sid, {
        "id": sid, "title": "带模型的会话", "workspace_id": "default",
        "model_id": pid, "reasoning": "low",
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，需要我做什么？"},
        ],
        "created_at": now, "updated_at": now,
    })
    # 模拟进程重启：内存里没有任何会话状态，只有一个全新的 _Session 对象
    sess = _w._Session(sid, wsrv)
    agent = sess.ensure_agent()
    assert sess.title == "带模型的会话", f"标题未恢复：{sess.title}"
    assert sess.model_id == pid
    assert agent.model == "qwen2.5:7b", f"应使用会话保存的模型，实际 {agent.model}"
    assert agent.reasoning_level == "low", f"推理等级未恢复：{agent.reasoning_level}"
    assert [m["content"] for m in agent.messages[1:]] == [
        "你好", "你好，需要我做什么？",
    ], f"历史未恢复：{agent.messages}"
    # 同一份数据只读一次：元数据与历史共用缓存
    assert sess._session_data is not None
    api("DELETE", f"/api/models/{pid}")
    print("  [11b] 重启后会话模型恢复：标题/模型/推理/历史 通过")


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


def scenario_user_memory_command() -> None:
    """用户显式「请帮我记住 XXX」：直接写用户画像，不依赖模型工具。

    覆盖：快速对话（无工具）也能写、重复指令命中 upsert、普通消息不触发。
    """
    import shutil

    import core.memory as mem

    tmp = tempfile.mkdtemp(prefix="ma-user-mem-")
    target = os.path.join(tmp, "user_profile.md")
    orig = mem.user_profile_path
    mem.user_profile_path = lambda: target   # type: ignore[assignment]
    try:
        events = collect({
            "message": "请帮我记住 我偏好简洁的中文回答",
            "mode": "chat", "web": False, "session": "t-mem-cmd",
        })
        saved = [e for e in events if e["type"] == "memory"]
        assert saved and saved[0]["ok"] is True, saved
        assert not saved[0].get("duplicate"), saved
        with open(target, encoding="utf-8") as f:
            body = f.read()
        assert "简洁的中文回答" in body and "[pref]" in body, body

        again = collect({
            "message": "请帮我记住 我偏好简洁的中文回答",
            "mode": "chat", "web": False, "session": "t-mem-cmd",
        })
        dup = [e for e in again if e["type"] == "memory"]
        assert dup and dup[0].get("duplicate") is True, dup

        plain = collect({
            "message": "你好", "mode": "chat", "web": False, "session": "t-mem-cmd",
        })
        assert not [e for e in plain if e["type"] == "memory"], plain
        print("  [16] 用户指令记忆：写入画像 / 重复 upsert / 普通消息不触发 通过")
    finally:
        mem.user_profile_path = orig
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_api_guards() -> None:
    """接口兜底：未知 /api/* 404、keep 非整数 400、非法会话 id 400、
    工作区 HTML/SVG 按附件 + 沙箱下发、文件夹选择框仅本机。
    """
    import core.webui as webui_mod

    # 未知 /api/* 必须 404（落到静态托管会返回 200 + index.html）
    status, body = api("GET", "/api/no_such_endpoint")
    assert status == 404 and "error" in body, f"未知接口应 404：{status} {body}"
    # 非 API 路径仍走静态托管（200/404 都可，但不能是 JSON 错误）
    try:
        with urllib.request.urlopen(
            Request(f"http://127.0.0.1:{WEB_PORT}/no_such_page"), timeout=10
        ) as resp:
            assert resp.status in (200, 404)
    except HTTPError as e:
        assert e.code == 404

    # keep 非整数 → 400（以前 int() 抛异常变 500）
    status, body = api("POST", "/api/sessions/t-guard/truncate", {"keep": "abc"})
    assert status == 400 and "error" in body, f"keep 非法应 400：{status} {body}"

    # 非法会话 id → 400（不能拼进文件系统）
    status, body = api("GET", "/api/history?session=" + quote("../../evil"))
    assert status == 400, f"非法 session 应 400：{status} {body}"
    status, body = api("POST", "/api/sessions/..%2Fevil/truncate", {"keep": 0})
    assert status == 400, f"非法路由 session 应 400：{status} {body}"

    # 工作区里的 .svg 仍可内联浏览，但必须带 CSP sandbox（禁脚本，防同源 XSS）
    _make_file("_guard_probe.svg")
    try:
        with urllib.request.urlopen(
            Request(f"http://127.0.0.1:{WEB_PORT}/api/workspace/asset/default/_guard_probe.svg"),
            timeout=10,
        ) as resp:
            assert resp.headers.get("Content-Type", "").startswith("image/svg+xml"), resp.headers
            assert resp.headers.get("Content-Security-Policy") == "sandbox allow-popups", resp.headers
            # 不能改成强制下载：预览里的静态资源仍要能内联加载
            assert "attachment" not in (resp.headers.get("Content-Disposition") or ""), resp.headers
    finally:
        os.remove(os.path.join(_ws_root(), "_guard_probe.svg"))

    # 文件夹选择框仅本机：把对端伪装成远端
    original = webui_mod._RequestHandler._is_remote
    webui_mod._RequestHandler._is_remote = lambda self: True
    try:
        status, body = api("POST", "/api/workspaces/pick", {})
        assert status == 403, f"远端弹选择框应 403：{status} {body}"
    finally:
        webui_mod._RequestHandler._is_remote = original
    print("  [16] 未知接口 404 / keep 400 / 会话 id 白名单 / SVG 沙箱 / 选择框限本机通过")


def scenario_keepalive_body() -> None:
    """⑭ keep-alive 复用：没读的请求体不能污染下一个请求。

    回归点：前端「设为当前 / 连接测试」会带 `{}` 请求体，而处理函数只用路径参数。
    HTTP/1.1 复用连接时若不消费这段字节，后续 GET 会被解析成畸形请求（400），
    前端表现就是「点一下按钮，模型列表突然变空」。
    """
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=15)
    try:
        conn.request("POST", f"/api/models/{_mock['id']}/activate", body=b"{}",
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


# ---------------------------------------------------------------- 运行模式

def scenario_run_mode_auto_login() -> None:
    """运行模式（MINI_AGENT_RUN_MODE）：默认生产；dev 模式仅本机回环免密自动登录。

    安全边界必须成立：服务器开了 dev 模式，但请求来自远程地址时仍按未登录处理。
    """
    import core.auth as _auth
    import core.webui as _webui

    port = 8777
    tmp = tempfile.mkdtemp(prefix="miniagent-auth-test-")
    old_db = _auth._DB_PATH
    old_env = os.environ.get(_webui._RUN_MODE_ENV)

    def get(path: str, cookie: str | None = None):
        headers = {"Cookie": cookie} if cookie else {}
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers.get("Set-Cookie") or ""
        except HTTPError as e:
            body = e.read() or b"{}"
            return e.code, json.loads(body or b"{}"), e.headers.get("Set-Cookie") or ""

    wsrv = None
    try:
        _auth._DB_PATH = os.path.join(tmp, "auth.db")
        _auth.init_db()
        _auth.ensure_default_admin()
        wsrv = _webui.WebServer(port=port, model="mock-gpt", login_enabled=True)
        threading.Thread(target=wsrv.serve_forever, daemon=True).start()
        for _ in range(50):
            try:
                if get("/api/health")[0] == 200:
                    break
            except Exception:  # noqa: BLE001 —— 服务还没起来
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("登录态测试服务未能在 5s 内就绪")

        # 生产模式（未设置 / 不填）：没 Cookie 就 401
        os.environ.pop(_webui._RUN_MODE_ENV, None)
        status, body, _ = get("/api/auth/me")
        assert status == 401 and body.get("code") == "UNAUTHENTICATED", (status, body)

        # 开发模式：本机访问免密登录 admin，且不再要求改初始密码
        os.environ[_webui._RUN_MODE_ENV] = "dev"
        status, body, set_cookie = get("/api/auth/me")
        user = body.get("user") or {}
        assert status == 200, (status, body)
        assert user.get("username") == _auth.DEFAULT_ADMIN, user
        assert user.get("role") == _auth.ROLE_SUPERADMIN, user
        assert user.get("must_change_password") is False, user
        assert "ma_session=" in set_cookie, set_cookie

        # 自动登录确实建立了会话：带 Cookie 访问其它接口放行
        status, _, _ = get("/api/state", set_cookie.split(";")[0])
        assert status == 200, status

        # 安全边界：远程来源即使在 dev 模式也必须登录
        original = _webui._RequestHandler._client_ip
        _webui._RequestHandler._client_ip = lambda self: "203.0.113.7"
        try:
            status, body, _ = get("/api/auth/me")
            assert status == 401, (status, body)
        finally:
            _webui._RequestHandler._client_ip = original
    finally:
        if wsrv is not None:
            wsrv.shutdown()
        if old_env is None:
            os.environ.pop(_webui._RUN_MODE_ENV, None)
        else:
            os.environ[_webui._RUN_MODE_ENV] = old_env
        _auth._DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)
    print("  [17] 运行模式：dev 本机自动登录 / 默认生产 / 远程不放行 通过")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    # 隔离存储：会话 / 工作区 / 设置都指到临时目录，
    # 既不污染真实 web_data，也让每次运行从干净状态开始
    # （models.json 已在模块导入时单独隔离并写入 mock 服务商）
    tmpdir = tempfile.mkdtemp(prefix="miniagent-webui-test-")
    import core.settings as _st
    import core.workspace_config as _wc
    import core.webui as _webui

    _webui._SESSIONS_DIR = os.path.join(tmpdir, "sessions")
    # 事件流是独立模块（core/events.py 自己算路径），也必须指到临时目录，
    # 否则测试会把 *.events.jsonl 写进真实 web_data/sessions。
    import core.events as _events

    _events._SESSIONS_DIR = os.path.join(tmpdir, "sessions")
    # models.json 已在导入时指向独立的临时文件（并写入 mock 服务商），这里不再改
    _wc._WORKSPACES_FILE = os.path.join(tmpdir, "workspaces.json")
    _st._SETTINGS_FILE = os.path.join(tmpdir, "settings.json")

    # mock OpenAI 兼容服务
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    # 被测的 Web UI 服务。登录校验另有用例覆盖，这里统一关掉，
    # 避免每个场景都要先走一遍登录、拿 Cookie。
    from core.webui import WebServer

    wsrv = WebServer(port=WEB_PORT, model="mock-gpt", login_enabled=False)
    threading.Thread(target=wsrv.serve_forever, daemon=True).start()
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
        ("健康检查", scenario_health),
        ("Host 头白名单", scenario_host_guard),
        ("运行模式-开发自动登录", scenario_run_mode_auto_login),
        ("附件路径解析", scenario_attachment_path),
        ("展示分组-注入不拆气泡", scenario_display_grouping),
        ("多选删除消息", scenario_delete_messages),
        ("会话标题生成与锁定", scenario_session_title),
        ("chat 模式 + history", scenario_chat_and_history),
        ("agent 模式工具轨迹", scenario_agent_tools),
        ("事件流轨迹回放", scenario_trajectory),
        ("停止任务", scenario_stop),
        ("页面断开不取消", scenario_detach_keeps_running),
        ("运行中指示器", scenario_running_indicator),
        ("接入后台任务", scenario_reconnect),
        ("危险确认-拒绝", lambda: scenario_confirm(deny=True)),
        ("危险确认-允许", lambda: scenario_confirm(deny=False)),
        ("同会话并发 409", scenario_409),
        ("state + 首页", scenario_state_and_index),
        ("工作区管理", scenario_workspaces),
        ("模型配置", scenario_models),
        ("重启后会话模型恢复", lambda: scenario_session_model_restore(wsrv)),
        ("会话搜索", scenario_search),
        ("通用配置", scenario_settings),
        ("用户指令记忆", scenario_user_memory_command),
        ("keep-alive 请求体", scenario_keepalive_body),
        ("接口兜底与限权", scenario_api_guards),
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
