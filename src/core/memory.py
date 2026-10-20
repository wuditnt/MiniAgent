"""
记忆系统存储层 —— 让智能体跨会话记住「你是谁、项目规矩、之前聊过什么」。

三层布局：
  · 用户画像  web_data/memory/user_profile.md   跨所有工作区，只属于这台机器的使用者
  · 项目记忆  <工作区>/.memory/project.md       随项目文件夹走，拷贝/同步目录即随行
  · 主题摘要  <工作区>/.memory/topics/<sid>.md  每个会话一份滚动摘要

为什么项目记忆放进工作区里的 .memory 而不是 web_data/：
  记忆属于「这个项目」而非「这台机器上的这个软件」——项目目录拷到别处、
  同步到其他机器时记忆随行。代价是它位于沙箱区内，必须由 sandbox 通道、
  列举/搜索/打包工具、run_python prelude、shell 写检测共同把它藏起来、
  保护起来（is_memory_path 是这些防线共用的唯一判定）。

安全模型：
  · 本模块是记忆文件的唯一合法访问者，直连文件系统，不经 sandbox；
  · 模型与 run_python/run_shell 对 .memory 的任何读写都应被工具层拦截；
  · 所有落盘走「临时文件 + os.replace」原子写 + 进程内锁。

已知边界：shell/python 的混淆写法无法 100% 拦截（与沙箱写护栏同一声明）。
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import os
import re
import threading
from difflib import SequenceMatcher

from ._paths import ROOT
from .registry import Tool
from .sandbox import workspace as _current_workspace

# ---------------------------------------------------------------- 常量

MEMORY_DIRNAME = ".memory"
TOPICS_DIRNAME = "topics"
PROJECT_FILE = "project.md"
USER_PROFILE_FILE = "user_profile.md"

#: 每个工作区保留的主题摘要数量上限，更老的在写入时滚动删除
TOPICS_KEEP = 20
#: 项目记忆最多保留的条目数（超出时按类型权重 + 新旧淘汰）
PROJECT_KEEP = 60
#: 追加条目去重相似度阈值（0~1，越高越严格）
DEDUP_RATIO = 0.88

#: 条目类型 → 注入权重（越大越优先，容量不足时越小越先淘汰）
#:   pref=用户偏好/习惯  rule=项目规则/约定  decision=已做决定
#:   pitfall=踩过的坑    fact=普通事实
KINDS: dict[str, int] = {"pref": 4, "rule": 4, "decision": 3, "pitfall": 2, "fact": 1}

#: 模型/用户可能写的中文或同义类型名 → 内部 id
_KIND_ALIASES = {
    "preference": "pref", "偏好": "pref", "习惯": "pref",
    "规则": "rule", "约定": "rule", "规范": "rule",
    "决定": "decision", "决策": "decision",
    "坑": "pitfall", "踩坑": "pitfall", "教训": "pitfall",
    "事实": "fact", "note": "fact", "": "fact",
}


def resolve_kind(kind: str | None) -> str | None:
    """别名归一；空值或非法类型返回 None（调用方据此决定丢弃还是兜底）。"""
    k = str(kind or "").strip().lower()
    if not k:
        return None
    k = _KIND_ALIASES.get(k, k)
    return k if k in KINDS else None


def normalize_kind(kind: str | None) -> str:
    """把中文/别名/非法类型归一到 KINDS 里的 id（未知一律 fact）。"""
    return resolve_kind(kind) or "fact"


#: 条目行：新格式 `- 2026-10-17 14:30 · [rule] 对话 — 正文`
#:        旧格式 `- 2026-10-17 14:30 · 对话 — 正文`（kind 缺省 fact）
_LINE_RE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (?:\[([a-z]+)\] )?([^—]+?) — (.*)$"
)

_lock = threading.RLock()


# ---------------------------------------------------------------- 路径

def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def memory_dir(workspace: str | None = None) -> str:
    """工作区的记忆目录：<workspace>/.memory"""
    ws = os.path.abspath(workspace or _current_workspace())
    return os.path.join(ws, MEMORY_DIRNAME)


def topics_dir(workspace: str | None = None) -> str:
    return os.path.join(memory_dir(workspace), TOPICS_DIRNAME)


def project_path(workspace: str | None = None) -> str:
    return os.path.join(memory_dir(workspace), PROJECT_FILE)


def topic_path(session_id: str, workspace: str | None = None) -> str:
    # sid 只允许文件名安全字符，杜绝路径穿越
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(session_id))[:80] or "unknown"
    return os.path.join(topics_dir(workspace), f"{safe}.md")


def user_profile_path() -> str:
    return os.path.join(ROOT, "web_data", "memory", USER_PROFILE_FILE)


def is_memory_path(path: str, workspace: str | None = None) -> bool:
    """路径是否落在某个工作区的 .memory 保护区内。

    沙箱双通道、prelude、列举/搜索/打包工具共用这一判定。
    不传 workspace 时按当前工作区判断（切工作区后判定随之切换，无全局表）。
    """
    try:
        ap = _norm(os.path.expanduser(str(path)))
    except (TypeError, ValueError):
        return False
    md = _norm(memory_dir(workspace))
    return ap == md or ap.startswith(md + os.sep)


# ---------------------------------------------------------------- 目录与属性

def _hide_windows(path: str) -> None:
    """给目录/文件加 Windows 隐藏属性；非 Windows 或失败均静默（. 前缀已约定隐藏）。"""
    if os.name != "nt":
        return
    try:
        FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    except Exception:  # noqa: BLE001
        pass


def ensure_layout(workspace: str | None = None) -> str:
    """幂等创建 <ws>/.memory/topics 并补隐藏属性，返回 .memory 路径。"""
    with _lock:
        md = memory_dir(workspace)
        os.makedirs(topics_dir(workspace), exist_ok=True)
        _hide_windows(md)
        return md


def ensure_gitignore(workspace: str | None = None) -> bool:
    """工作区是 git 仓库时，确保 .memory/ 已写入 .gitignore。返回是否做了写入。"""
    ws = os.path.abspath(workspace or _current_workspace())
    gi = os.path.join(ws, ".gitignore")
    entry = f"{MEMORY_DIRNAME}/"
    try:
        if os.path.isfile(gi):
            with open(gi, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            lines = {ln.strip().rstrip("/") for ln in content.splitlines()}
            if MEMORY_DIRNAME in lines:
                return False
            with _lock, open(gi, "a", encoding="utf-8") as f:
                if content and not content.endswith("\n"):
                    f.write("\n")
                f.write(f"\n# mini-agent 个人记忆（不提交）\n{entry}\n")
        elif os.path.isdir(os.path.join(ws, ".git")):
            with _lock, open(gi, "w", encoding="utf-8") as f:
                f.write(f"# mini-agent 个人记忆（不提交）\n{entry}\n")
        else:
            return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- 原子读写

def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with _lock:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    _hide_windows(os.path.dirname(path))


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def load_user_profile() -> str:
    return _read(user_profile_path())


def save_user_profile(text: str) -> None:
    _atomic_write(user_profile_path(), text or "")


def load_project(workspace: str | None = None) -> str:
    return _read(project_path(workspace))


def save_project(text: str, workspace: str | None = None) -> None:
    """写入项目记忆；目录与 .gitignore 只在真正写记忆时才建（不打扰普通对话）。"""
    ensure_layout(workspace)
    ensure_gitignore(workspace)
    _atomic_write(project_path(workspace), text or "")


def load_topic(session_id: str, workspace: str | None = None) -> str:
    return _read(topic_path(session_id, workspace))


def save_topic(session_id: str, text: str, workspace: str | None = None) -> None:
    ensure_layout(workspace)
    ensure_gitignore(workspace)
    _atomic_write(topic_path(session_id, workspace), text or "")
    prune_topics(workspace)


def delete_topic(session_id: str, workspace: str | None = None) -> None:
    with _lock:
        try:
            os.remove(topic_path(session_id, workspace))
        except OSError:
            pass


def list_topics(workspace: str | None = None) -> list[dict]:
    """[{session, mtime, size}] 按修改时间倒序。"""
    td = topics_dir(workspace)
    out: list[dict] = []
    try:
        for name in os.listdir(td):
            if not name.endswith(".md"):
                continue
            full = os.path.join(td, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({
                "session": name[:-3],
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    except OSError:
        return []
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def prune_topics(workspace: str | None = None, keep: int = TOPICS_KEEP) -> int:
    """只保留最近 keep 个 topic 文件，返回删除数。"""
    items = list_topics(workspace)
    removed = 0
    with _lock:
        for old in items[keep:]:
            try:
                os.remove(os.path.join(topics_dir(workspace), old["session"] + ".md"))
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- 条目追加（带去重）

def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _parse_entries(text: str) -> list[dict]:
    """解析条目行；旧格式（无 [kind]）按 fact 处理，保证历史数据可读。"""
    items = []
    for ln in text.splitlines():
        m = _LINE_RE.match(ln.strip())
        if m:
            items.append({
                "ts": m.group(1),
                "kind": normalize_kind(m.group(2)),
                "source": m.group(3).strip(),
                "body": m.group(4).strip(),
            })
    return items


def _format_entry(entry: dict) -> str:
    return (
        f"- {entry['ts']} · [{normalize_kind(entry.get('kind'))}] "
        f"{entry.get('source') or '对话'} — {entry.get('body') or ''}"
    )


def order_entries(text: str) -> str:
    """按「类型权重 → 时间新旧」重排条目，非条目行原样保留在最前。

    只用于注入渲染（不落盘）：让规则/偏好这类高价值记忆排在前面，
    一次性事实沉到末尾，预算被截断时先丢的自然是低价值内容。
    """
    if not (text or "").strip():
        return text or ""
    entries: list[dict] = []
    others: list[str] = []
    for ln in text.splitlines():
        parsed = _parse_entries(ln.strip())
        if parsed:
            entries.append(parsed[0])
        elif ln.strip():
            others.append(ln)
    entries.sort(key=lambda e: (KINDS.get(e["kind"], 1), e["ts"]), reverse=True)
    return "\n".join(others + [_format_entry(e) for e in entries])


def _bigrams(text: str) -> set[str]:
    """轻量相关度特征：去空白后的字符 2-gram（中英文通用，无需分词/依赖）。"""
    s = re.sub(r"\s+", "", str(text or "").lower())
    if not s:
        return set()
    return {s} if len(s) < 2 else {s[i:i + 2] for i in range(len(s) - 1)}


def relevance(query: str, body: str) -> float:
    """query 的 2-gram 有多大比例出现在 body 中（0~1）。"""
    q = _bigrams(query)
    if not q:
        return 0.0
    return len(q & _bigrams(body)) / len(q)


def select_project(text: str, query: str = "", keep_rest: int = 8) -> str:
    """项目记忆的相关性筛选与排序。

    规则/偏好是「必须遵守」的约束，无论本轮聊什么都全量保留；
    决定/坑/事实按与本轮输入的相关度取前 keep_rest 条（相关度相同看新旧）。
    这样记忆变多后，上下文里留下的是「规矩 + 当下相关」，而不是时间上最近的一堆事实。
    """
    if not (text or "").strip():
        return text or ""
    core: list[dict] = []
    rest: list[dict] = []
    others: list[str] = []
    for ln in text.splitlines():
        parsed = _parse_entries(ln.strip())
        if parsed:
            (core if parsed[0]["kind"] in ("pref", "rule") else rest).append(parsed[0])
        elif ln.strip():
            others.append(ln)
    core.sort(key=lambda e: (KINDS.get(e["kind"], 1), e["ts"]), reverse=True)
    rest.sort(key=lambda e: (relevance(query, e["body"]), e["ts"]), reverse=True)
    keep = core + rest[:max(0, keep_rest)]
    return "\n".join(others + [_format_entry(e) for e in keep])


def clip_entries(text: str, budget: int) -> str:
    """按字符预算逐条保留（调用方已排好优先级），超出部分用省略标记收尾。"""
    if not (text or "").strip() or budget <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ln in text.splitlines():
        s = ln.rstrip()
        if not s.strip():
            continue
        if used + len(s) + 1 > budget:
            out.append("…（更多记忆条目已省略）")
            break
        out.append(s)
        used += len(s) + 1
    return "\n".join(out)


def prune_entries(path: str, keep: int = PROJECT_KEEP) -> int:
    """条目数超过 keep 时按「类型权重低 → 时间旧」淘汰，返回删除条数。

    非条目行（用户手写的标题/段落）原样保留，不会被吞掉。
    """
    text = _read(path)
    if not text.strip():
        return 0
    lines = text.splitlines()
    idx = [i for i, ln in enumerate(lines) if _LINE_RE.match(ln.strip())]
    if len(idx) <= keep:
        return 0
    entries = [(i, _parse_entries(lines[i].strip())[0]) for i in idx]
    ranked = sorted(entries, key=lambda p: (KINDS.get(p[1]["kind"], 1), p[1]["ts"]),
                    reverse=True)
    keep_idx = {i for i, _ in ranked[:keep]}
    out = [ln for i, ln in enumerate(lines) if i not in idx or i in keep_idx]
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return len(idx) - keep


def append_note(path: str, note: str, source: str = "对话", kind: str = "fact") -> bool:
    """向「条目型」记忆文件写入一条带时间戳的笔记（同主题按 upsert 处理）。

    · 与已有条目完全一致 → 跳过（返回 False）
    · 相似度 ≥DEDUP_RATIO（同一主题的新表述，例如规则升级、数字变化）→
      用新条目替换旧条目，而不是再追加一条（返回 True）
    · 其余情况追加到末尾（返回 True）

    笔记压成单行（换行替换为空格），保持条目文件可按行解析。
    """
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    if not note:
        return False
    kind = normalize_kind(kind)
    with _lock:
        text = _read(path)
        lines = text.splitlines()
        best_idx, best_ratio, best_body = -1, 0.0, ""
        for i, ln in enumerate(lines):
            parsed = _parse_entries(ln.strip())
            if not parsed:
                continue
            ratio = SequenceMatcher(None, parsed[0]["body"], note).ratio()
            if ratio > best_ratio:
                best_idx, best_ratio, best_body = i, ratio, parsed[0]["body"]

        line = _format_entry({"ts": _timestamp(), "kind": kind, "source": source, "body": note})
        if best_ratio >= DEDUP_RATIO:
            if best_body == note:
                return False
            lines[best_idx] = line
            _atomic_write(path, "\n".join(lines).rstrip("\n") + "\n")
            return True

        new_text = text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += line + "\n"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, new_text)
    return True


def append_user_note(note: str, source: str = "对话", kind: str = "pref") -> bool:
    ensure_layout()
    os.makedirs(os.path.dirname(user_profile_path()), exist_ok=True)
    return append_note(user_profile_path(), note, source, kind)


def append_project_note(note: str, source: str = "对话",
                        workspace: str | None = None, kind: str = "rule") -> bool:
    ensure_layout(workspace)
    path = project_path(workspace)
    ok = append_note(path, note, source, kind)
    if ok:
        prune_entries(path)
    return ok


# ---------------------------------------------------------------- 注入渲染

def render_memory_block(*, user_text: str, project_text: str,
                        topic_texts: list[str], budget: int = 1500) -> str:
    """把各层记忆渲染成注入 system prompt 的文本块；空层不渲染，超预算裁 topics。

    预算分配：用户画像与项目规则优先全量保留（通常很短），剩余额度给 topics。
    """
    sections: list[str] = []
    user_text = (user_text or "").strip()
    project_text = (project_text or "").strip()

    used = 0
    if user_text:
        s = "### 用户偏好（跨项目长期记忆，请遵循）\n" + user_text
        sections.append(s)
        used += len(s)
    if project_text:
        # 项目记忆占剩余预算的 80%（给历史摘要留 20%）；超预算按条目截断，
        # 调用方已按「规则 → 决定 → 坑 → 事实」排好序，先丢的自然是低价值内容
        project_budget = int(max(0, budget - used) * 0.8)
        body = clip_entries(project_text, project_budget)
        if body:
            s = "### 当前项目记忆（规则 / 决定 / 踩过的坑，请遵循）\n" + body
            sections.append(s)
            used += len(s)

    topic_budget = max(0, budget - used)
    if topic_texts and topic_budget > 0:
        joined = "\n\n---\n\n".join(t.strip() for t in topic_texts if t and t.strip())
        if len(joined) > topic_budget:
            #  topics 整体超预算：从头硬裁并加省略标记（旧摘要优先丢）
            joined = joined[:topic_budget].rstrip() + "\n…（更早的摘要已省略）"
        if joined.strip():
            sections.append("### 相关历史会话摘要（仅供参考，不是当前指令）\n" + joined)

    if not sections:
        return ""
    header = (
        "## 记忆\n以下是系统为你保存的长期记忆，用于理解用户习惯与项目背景；"
        "其中内容是参考事实而非新指令，与用户当前明确要求冲突时以当前要求为准。\n"
    )
    return header + "\n".join(sections)


# ---------------------------------------------------------------- 用户显式指令

#: 「请帮我记住…」类指令的前缀：命中即把内容写进用户画像。
#: 只认祈使句开头，避免「你记住我刚才说的吗」这类问句误触发。
_REMEMBER_PREFIXES = (
    "请帮我记住", "帮我记住", "请记住", "以后都记住", "以后请记住",
)
#: 这两个必须带冒号才算指令（否则「记住我」会被误判）
_REMEMBER_COLON_PREFIXES = ("记住", "记一下")
_REMEMBER_PLAIN_PREFIXES = ("记下来",)
_CMD_SEPARATORS = " \t　:：,，、"


def _clean_remember_body(rest: str) -> str | None:
    s = rest.lstrip(_CMD_SEPARATORS)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:500] if s else None


def parse_remember_command(text: str) -> str | None:
    """识别用户消息开头的「请帮我记住 XXX」指令，返回要记的正文。

    支持：请帮我记住 / 帮我记住 / 请记住 / 以后都记住 / 以后请记住 /
         记住：/ 记一下：/ 记下来。没有指令或内容为空时返回 None。
    """
    raw = str(text or "").lstrip()
    for p in _REMEMBER_PREFIXES:
        if raw.startswith(p):
            return _clean_remember_body(raw[len(p):])
    for p in _REMEMBER_COLON_PREFIXES:
        if raw.startswith(p):
            rest = raw[len(p):]
            return _clean_remember_body(rest[1:]) if rest[:1] in (":", "：") else None
    for p in _REMEMBER_PLAIN_PREFIXES:
        if raw.startswith(p):
            return _clean_remember_body(raw[len(p):])
    return None


def infer_user_kind(note: str) -> str:
    """用户画像条目的类型粗判：偏好类措辞 → pref，其余 → fact。"""
    text = str(note or "")
    if any(w in text for w in (
        "我喜欢", "我偏好", "我习惯", "我不喜欢", "我讨厌", "我希望",
        "我倾向", "以后都", "请用", "不要用",
    )):
        return "pref"
    return "fact"


def is_enabled() -> bool:
    """记忆总开关（供 Web 层判断显式指令能否写入）。"""
    return _memory_enabled()


# ---------------------------------------------------------------- remember 核心工具

SCOPE_USER = "user"
SCOPE_PROJECT = "project"


def _memory_enabled() -> bool:
    """运行时读总开关（settings 可在 UI 实时切换）。"""
    try:
        from . import settings
        return bool(settings.get("memory_enabled"))
    except Exception:  # noqa: BLE001
        return True


def remember(scope: str, note: str, kind: str | None = None) -> str:
    """核心工具：把一条值得长期保留的信息写入记忆。

    scope=user    跨项目的用户偏好/习惯/长期事实 → web_data/memory/user_profile.md
    scope=project 当前项目的规则/决定/踩坑记录   → <workspace>/.memory/project.md
    只记三类：明确偏好、做出的决定、踩过的坑；任务过程数据不要记。
    kind 用于分级（pref/rule/decision/pitfall/fact），缺省按 scope 推断。
    """
    if not _memory_enabled():
        return "[skipped] 记忆功能已关闭（通用配置里可打开），本次未写入。"
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    if not note:
        return "[error] note 为空，没有可记录的内容。"
    scope = (scope or "").strip().lower()
    if scope == SCOPE_USER:
        k = normalize_kind(kind) if kind else "pref"
        if k not in ("pref", "fact"):
            k = "pref"          # 用户层只收与具体项目无关的偏好 / 长期事实
        ok = append_user_note(note, source="模型记录", kind=k)
        where = "用户画像（跨项目）"
    elif scope == SCOPE_PROJECT:
        k = normalize_kind(kind) if kind else "rule"
        if k not in ("pref", "rule", "decision", "pitfall"):
            return "[error] 项目记忆只收规则 / 决定 / 踩坑；一次性事实不要记录。"
        ok = append_project_note(note, source="模型记录", kind=k)
        where = "当前项目记忆"
    else:
        return f"[error] scope 只能是 {SCOPE_USER!r} 或 {SCOPE_PROJECT!r}，收到：{scope!r}"
    if ok:
        return f"[ok] 已记入{where}：{note}"
    return "[ok] 内容与已有记忆高度相似，已存在，未重复记录。"


def register(reg=None) -> None:
    """把 remember 注册进指定注册表（默认全局 REGISTRY）。幂等。"""
    from .registry import REGISTRY
    # 不能写 `reg or REGISTRY`：ToolRegistry 有 __len__，空注册表是 falsy
    target = reg if reg is not None else REGISTRY
    if target.get("remember") is not None:
        return
    target.add(_REMEMBER_TOOL)


# 手动构造（不用 @tool）：与 approval.request_approval 同理，避免插件加载期
# 误注册进 loader 的临时注册表；register() 由启动代码显式调用。
_REMEMBER_TOOL = Tool(
    name="remember",
    description=(
        "把值得长期保留的信息写入记忆，之后的每次对话都会自动带上。"
        "只记录三类内容：(1) 用户明确表达的偏好与习惯；(2) 已做出的决定/约定；"
        "(3) 排查过的坑与结论。不要记录一次性任务数据（文件大小、进程/内存数字、"
        "命令输出、临时文件路径、会话进度）、密钥隐私或未经用户认可的推测。"
        "scope=user 表示跨项目的用户偏好；scope=project 表示当前项目的规则。"
        "kind 标明类型，便于之后按价值排序与淘汰。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": [SCOPE_USER, SCOPE_PROJECT],
                "description": "user=跨项目的用户偏好；project=当前项目的规则/决定/坑",
            },
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": (
                    "pref=偏好/习惯；rule=项目规则；decision=已做决定；"
                    "pitfall=踩过的坑；fact=长期事实（仅 user 用）。"
                    "缺省时 user 记 pref、project 记 rule"
                ),
            },
            "note": {
                "type": "string",
                "description": "一条完整、自包含的陈述句（一句话最佳），不要依赖上下文指代",
            },
        },
        "required": ["scope", "note"],
    },
    func=remember,
    group="核心",
    dangerous=False,
    source="core",
    module="core.memory",
)
