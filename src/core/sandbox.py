"""
沙箱与安全策略：所有工具都必须经过这一层。

为什么把沙箱独立成一个模块？
    因为「工具」会不断被扩展，而「边界」必须只有一处定义。
    插件作者只要 `from core import safe_path`，就自动继承了工作区限制；
    核心代码则完全不需要知道任何一个具体插件长什么样。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from ._paths import DEFAULT_WORKSPACE
from .cancel import current_token, kill_tree

# ---------------------------------------------------------------- 可调参数

# 没设 AGENT_WORKSPACE 时的兜底。为什么不是 os.getcwd()？
#   因为 cwd 取决于「用户在哪敲的命令」，而从任意目录跑 `python -m core`
#   都是合法用法。锚到 DEFAULT_WORKSPACE（用户数据目录），行为才可预测。
#   （测试通过 AGENT_WORKSPACE 把它指回项目根，好让 demo_case/ 里的用例生效。）
_WORKSPACE = os.path.abspath(os.environ.get("AGENT_WORKSPACE") or DEFAULT_WORKSPACE)
MAX_OUTPUT_CHARS = int(os.environ.get("AGENT_MAX_OUTPUT", "8000"))
ALLOW_DANGEROUS = os.environ.get("AGENT_ALLOW_DANGEROUS") == "1"

# 兼容老写法：`tools.WORKSPACE`（导入时快照）
WORKSPACE = _WORKSPACE


def workspace() -> str:
    """当前工作区根目录 —— 唯一可信来源。"""
    return _WORKSPACE


def set_workspace(path: str) -> str:
    """运行时切换工作区。请在加载插件之前调用。"""
    global _WORKSPACE, WORKSPACE
    _WORKSPACE = os.path.abspath(path)
    WORKSPACE = _WORKSPACE
    return _WORKSPACE


def set_max_output_chars(n: int) -> int:
    """运行时调整工具输出上限（Web UI 的「通用配置」改完立刻生效）。"""
    global MAX_OUTPUT_CHARS
    MAX_OUTPUT_CHARS = max(1, n)
    return MAX_OUTPUT_CHARS


def max_output_chars() -> int:
    """当前工具输出上限 —— 读模块变量而不是导入时快照，保证改完立刻生效。"""
    return MAX_OUTPUT_CHARS


# ---------------------------------------------------------------- 沙箱读取策略

# strict   = 沙箱外一律不可读（默认）
# whitelist = 沙箱外仅 _READ_WHITELIST 内的路径可读
# open     = 沙箱外全部只读放开（写仍由 safe_path 硬禁）
_READ_MODE = "strict"
_READ_WHITELIST: tuple[str, ...] = ()


def set_read_policy(mode: str, whitelist) -> None:
    """运行时切换沙箱外读取策略（Web UI 保存配置后立即生效）。"""
    global _READ_MODE, _READ_WHITELIST
    _READ_MODE = mode if mode in ("strict", "whitelist", "open") else "strict"
    _READ_WHITELIST = tuple(
        os.path.normcase(os.path.abspath(os.path.expanduser(str(p))))
        for p in (whitelist or [])
        if str(p).strip()
    )


def read_mode() -> str:
    return _READ_MODE


def _within(target: str, root: str) -> bool:
    """target 是否等于 root 或位于 root 之内（大小写按操作系统规范化）。"""
    t = os.path.normcase(os.path.abspath(target))
    r = os.path.normcase(os.path.abspath(root))
    return t == r or t.startswith(r + os.sep)


def _whitelisted(ap: str) -> bool:
    target = os.path.normcase(os.path.abspath(ap))
    return any(target == w or target.startswith(w + os.sep) for w in _READ_WHITELIST)


# ---------------------------------------------------------------- 路径沙箱

class SandboxError(PermissionError):
    """路径越界。"""


def _protected(ap: str) -> bool:
    """是否命中工作区内的系统保护区（.memory 记忆目录）。

    延迟导入避免 sandbox ↔ memory 模块循环；memory 自身不依赖 sandbox 运行期状态。
    """
    from .memory import is_memory_path
    return is_memory_path(ap, _WORKSPACE)


def safe_path(p: str) -> str:
    """写通道：把相对路径解析到工作区内，并阻止一切越界访问（读/写都拦）。

    这是所有「会修改文件系统」的工具必须走的入口；读类工具请改用 read_path，
    后者会按沙箱读取策略有条件放开沙箱外只读访问。
    工作区内的系统保护区（.memory）在两个通道里都不可访问 —— 记忆只能由
    core.memory 模块自己读写。
    """
    raw = p if os.path.isabs(p) else os.path.join(_WORKSPACE, p)
    ap = os.path.abspath(raw)
    if not _within(ap, _WORKSPACE):
        raise SandboxError(f"路径越界：{ap} 不在工作区 {_WORKSPACE} 内")
    if _protected(ap):
        raise PermissionError(f".memory 是系统记忆目录，禁止工具访问：{ap}")
    return ap


def read_path(p: str) -> str:
    """读通道：相对路径锚定工作区；沙箱外绝对路径按读取策略判定。

      · 工作区内：始终放行（.memory 保护区除外）；
      · 工作区外：strict 拦截 / whitelist 仅白名单 / open 全部只读放开。
    返回解析后的绝对路径；越界时抛 SandboxError。
    """
    raw = p if os.path.isabs(p) else os.path.join(_WORKSPACE, p)
    ap = os.path.abspath(raw)
    if _within(ap, _WORKSPACE):
        if _protected(ap):
            raise PermissionError(f".memory 是系统记忆目录，禁止工具访问：{ap}")
        return ap
    if _READ_MODE == "open":
        return ap
    if _READ_MODE == "whitelist" and _whitelisted(ap):
        return ap
    if _READ_MODE == "whitelist":
        raise SandboxError(
            f"路径越界：{ap} 不在工作区 {_WORKSPACE} 或只读白名单内"
        )
    raise SandboxError(
        f"路径越界：{ap} 不在工作区 {_WORKSPACE} 内"
        f"（沙箱外默认只读关闭，可在「通用配置」放开白名单/只读模式）"
    )


def assert_can_write(p: str) -> str:
    """写护栏的统一判定：写操作（含删除/移动/重命名）的目标必须在工作区内。

    给 run_python 的同进程护栏与命令检测复用 —— 与 safe_path 的区别只是
    错误信息面向「脚本越权」场景。返回规范化绝对路径。
    """
    ap = os.path.abspath(os.path.expanduser(p))
    if not _within(ap, _WORKSPACE):
        raise PermissionError(
            f"沙箱外路径只读，禁止写入/删除：{ap}（工作区 {_WORKSPACE}）"
        )
    if _protected(ap):
        raise PermissionError(f".memory 是系统记忆目录，禁止写入/删除：{ap}")
    return ap


# ---------------------------------------------------------------- 输出截断

def clip(text: str | None, limit: int | None = None) -> str:
    """截断过长输出 —— 上下文管理的第一道闸门。

    真实 Agent 里，一次 `cat 大文件` 就足以撑爆上下文窗口，
    而这个开销会在后续每一轮被重复计费。
    """
    if text is None:
        return ""
    limit = limit or MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    omitted = len(text) - limit
    return f"{head}\n\n… [已截断 {omitted} 字符，共 {len(text)} 字符] …\n\n{tail}"


# ---------------------------------------------------------------- 字节处理

def read_bytes(path: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    with open(path, "rb") as f:
        return f.read(max_bytes)


def looks_binary(data: bytes) -> bool:
    """含 NUL 字节基本就判定为二进制。"""
    return b"\x00" in data[:8192]


def decode_bytes(data: bytes) -> tuple[str, str]:
    """按 UTF-8 → GBK → Latin-1 顺序尝试解码，返回 (文本, 实际编码)。"""
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), "latin-1"


def hexdump(data: bytes, limit: int = 512) -> str:
    """生成十六进制预览（二进制降级时给模型看的）。"""
    head = data[:limit]
    return "\n".join(
        f"{i:08x}  {' '.join(f'{b:02x}' for b in head[i:i + 16])}"
        for i in range(0, len(head), 16)
    )


# ---------------------------------------------------------------- 命令黑名单

_BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+[/~]",
    r"\bformat\b",
    r"del\s+/[sfq]\b",
    r"shutdown\b",
    r"mkfs",
    r":\(\)\{",
    r"diskpart",
    r"reg\s+delete",
    r"rd\s+/s",
]


def check_command(command: str) -> str | None:
    """危险命令检查。返回 None 表示放行，否则返回拦截原因。

    注意：这是「兜底」，不是「主要防线」。
    主要防线是控制台层面的交互式确认（见 repl.py 的 /approve）。
    """
    if ALLOW_DANGEROUS:
        return None
    for pat in _BLOCKED_PATTERNS:
        if re.search(pat, command, re.I):
            return (
                f"该命令命中危险模式 `{pat}`，已被拦截。\n"
                f"确认要执行请设置环境变量 AGENT_ALLOW_DANGEROUS=1 后重试。"
            )
    return None


# ---------------------------------------------------------------- shell 越界写检测

# 整条语句里「所有路径参数都必须在工作区内」的写动词（含删除/移动）
_SHELL_WRITE_ALL = ("del", "erase", "rmdir", "rd", "move", "ren", "rename",
                    "mkdir", "md", "mklink", "rm", "cacls", "icacls", "takeown")
# 多源一目标：只强制最后一个路径（目标）在工作区内
_SHELL_WRITE_DST = ("copy", "xcopy", "robocopy", "cp", "mv")

_PATH_TOKEN = re.compile(r'^([A-Za-z]:[\\/]|\\\\)')  # 盘符绝对路径 / UNC
_REDIRECT = re.compile(r'(?<![\w\d])>{1,2}\s*("?)([^\s&|<>"]+)\1')


def _token_escapes(token: str) -> bool:
    """路径 token 是否写到工作区外。只识别绝对路径与含 .. 的相对路径，
    普通相对路径（锚定 cwd=工作区）不会越界，不拦。"""
    t = token.strip().strip('"')
    if not t:
        return False
    if t.startswith("-") or re.match(r"^/[A-Za-z]", t):
        return False  # -f /F 之类的命令开关
    if _PATH_TOKEN.match(t):
        raw = t
    elif ".." in t:
        raw = os.path.join(_WORKSPACE, t)  # 相对路径只有含 .. 才可能逃逸
    else:
        return False
    return not _within(os.path.abspath(raw), _WORKSPACE)


def _token_hits_memory(token: str) -> bool:
    """路径 token 是否指向工作区的 .memory 保护区（相对/绝对都查）。"""
    t = token.strip().strip('"')
    if not t or t.startswith("-") or re.match(r"^/[A-Za-z]", t):
        return False
    if _PATH_TOKEN.match(t):
        raw = t
    else:
        raw = os.path.join(_WORKSPACE, t)
    from .memory import is_memory_path
    return is_memory_path(os.path.abspath(raw), _WORKSPACE)


def check_command_write(command: str) -> str | None:
    """检测 shell 命令里「写/删/移动到工作区外」或「触碰 .memory 保护区」的明显写法。

    覆盖：输出重定向（> / >>）与常见写动词（copy/move/del/rd/mkdir/...）。
    拦不住 powershell -c、程序内部写文件等混淆形式 —— 与 run_python 护栏
    同属「提高随手绕过成本」，不是强隔离边界。
    """
    # 1) 重定向目标（2>&1 这类目标是 &1，会被路径判定自然放过）
    for m in _REDIRECT.finditer(command):
        tok = m.group(2)
        if tok in ("&1", "&2", "NUL", "/dev/null"):
            continue
        if _token_escapes(tok):
            return f"检测到重定向写入工作区外路径：{tok}（沙箱外只读，请改写到工作区内）"
        if _token_hits_memory(tok):
            return f"检测到重定向写入系统记忆目录：{tok}（.memory 受保护，禁止工具访问）"

    # 2) 按 & 拆语句，看句首写动词
    for stmt in re.split(r"&&|&|\|\|", command):
        s = stmt.strip()
        if not s:
            continue
        head = re.split(r"[\s=]", s, 1)[0].strip('"').lower()
        head = os.path.basename(head)  # 兼容 C:\Windows\System32\xcopy.exe 形态
        head = os.path.splitext(head)[0]
        if head not in _SHELL_WRITE_ALL and head not in _SHELL_WRITE_DST:
            continue
        # 粗分词：去引号，只保留像路径的 token（绝对路径 / 含 .. / .memory）
        tokens = [t.strip('"') for t in re.split(r"\s+", s)[1:] if t.strip()]
        path_like = [t for t in tokens if _PATH_TOKEN.match(t) or ".." in t]
        if head in _SHELL_WRITE_DST and path_like:
            path_like = [path_like[-1]]  # copy/xcopy/robocopy：只管目标
        for tok in path_like:
            if _token_escapes(tok):
                return (
                    f"写操作 `{head}` 的路径在工作区外：{tok}"
                    f"（沙箱外只读，硬规则不可放开）"
                )
        # 保护区检查对全部非开关 token 做（相对锚定工作区，普通目录不会误伤）
        for tok in tokens:
            if _token_hits_memory(tok):
                return (
                    f"写操作 `{head}` 指向系统记忆目录：{tok}"
                    f"（.memory 受保护，禁止工具访问）"
                )
    return None


# ---------------------------------------------------------------- 可取消子进程

def run_process(
    cmd,
    *,
    shell: bool = False,
    cwd: str | None = None,
    timeout: float = 120,
    poll_interval: float = 0.15,
) -> tuple[int, str, str, bool]:
    """运行一个可被取消的子进程，返回 (returncode, stdout, stderr, was_cancelled)。

    为什么不用 subprocess.run？
      run() 会把主线程/工作线程**死死阻塞**在内部 wait 上：
        1. Ctrl+C 打不断（尤其 Windows），取消令牌也插不进去；
        2. 就算打断了，子进程还活着，在后台继续占资源。
    这里改用「Popen + 短轮询」：每 poll_interval 秒醒一次，
      · 发现令牌取消 → 杀掉整棵进程树，was_cancelled=True 返回；
      · 发现超时   → 杀掉整棵树，抛内建 TimeoutError。
    短轮询还有个副作用：每次 communicate(timeout=…) 醒来时 Python 都有机会
    处理信号，主线程场景下 Ctrl+C 也能及时送达。
    """
    token = current_token()
    if token is not None:
        token.check()   # 还没开始就被取消 —— 不必启动

    p = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # POSIX：让子进程自成进程组，killpg 才能一次杀干净
        start_new_session=(sys.platform != "win32"),
    )
    if token is not None:
        token.register_process(p)

    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = p.communicate(timeout=poll_interval)
                return int(p.returncode or 0), out or "", err or "", False
            except subprocess.TimeoutExpired:
                pass   # 没死透就继续轮询；已读到的输出不会丢
            if token is not None and token.cancelled():
                kill_tree(p)
                out, err = p.communicate()
                code = p.returncode if p.returncode is not None else -1
                return int(code), out or "", err or "", True
            if time.monotonic() >= deadline:
                kill_tree(p)
                p.communicate()
                # 对外抛内建 TimeoutError —— 调用方不用 import subprocess 也能接住
                raise TimeoutError(f"子进程超时（{timeout}s）")
    except BaseException:
        # 关键兜底：Ctrl+C 打断主线程轮询时，KeyboardInterrupt 会直接穿透到这里，
        # 而 finally 只负责 unregister —— 不补一刀的话子进程就成了孤儿。
        # BaseException 同时覆盖 KeyboardInterrupt / SystemExit / 协作式 Cancelled。
        kill_tree(p)
        try:
            p.communicate(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        if token is not None:
            token.unregister_process(p)
