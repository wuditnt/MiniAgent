"""
沙箱与安全策略：所有工具都必须经过这一层。

为什么把沙箱独立成一个模块？
    因为「工具」会不断被扩展，而「边界」必须只有一处定义。
    插件作者只要 `from core import safe_path`，就自动继承了工作区限制；
    核心代码则完全不需要知道任何一个具体插件长什么样。
"""

from __future__ import annotations

import contextvars
import os
import re
import subprocess
import sys
import time

from . import envs
from ._paths import DEFAULT_WORKSPACE
from .cancel import current_token, kill_tree

# ---------------------------------------------------------------- 可调参数

# 没设 MINI_AGENT_WORKSPACE 时的兜底。为什么不是 os.getcwd()？
#   因为 cwd 取决于「用户在哪敲的命令」，而从任意目录跑 `python -m core`
#   都是合法用法。锚到 DEFAULT_WORKSPACE（用户数据目录），行为才可预测。
#   （测试通过 MINI_AGENT_WORKSPACE 把它指回项目根，好让 demo_case/ 里的用例生效。）
#
# 用 ContextVar 而不是模块全局：Web 层每个会话一个 worker 线程，
# 并发会话各用各的工作区根；并行工具线程由 agent 用 copy_context 传播。
_WORKSPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_workspace",
    default=os.path.abspath(
        envs.get_str("MINI_AGENT_WORKSPACE", alias="AGENT_WORKSPACE") or DEFAULT_WORKSPACE
    ),
)
MAX_OUTPUT_CHARS = envs.get_int("MINI_AGENT_MAX_OUTPUT", 8000, alias="AGENT_MAX_OUTPUT")
ALLOW_DANGEROUS = envs.get_bool("MINI_AGENT_ALLOW_DANGEROUS", False, alias="AGENT_ALLOW_DANGEROUS")

# 兼容老写法：`tools.WORKSPACE`（导入时快照，运行期切换不会改它）
WORKSPACE = _WORKSPACE.get()


def workspace() -> str:
    """当前工作区根目录 —— 唯一可信来源（按运行上下文隔离）。"""
    return _WORKSPACE.get()


def set_workspace(path: str) -> str:
    """切换当前上下文的工作区（只影响本线程/本 run）。"""
    ap = os.path.abspath(path)
    _WORKSPACE.set(ap)
    return ap


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


def _real(p) -> str:
    """把路径规范化成「可比较的真实路径」：展开 ~ 、解析符号链接、按系统规则统一大小写。

    为什么必须 realpath：abspath 只是字符串折叠 —— 工作区里放一个指向外部的
    符号链接，abspath 结果仍在工作区前缀内，边界判定就整个失效了。
    realpath 不要求路径存在（不存在的尾部按字面保留），新建文件同样可判定。
    """
    return os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(p))))


def _under(t: str, r: str) -> bool:
    """t 是否等于 r 或位于 r 之下；按目录边界比较，避免 `ws` 误配 `ws-evil`。"""
    if t == r:
        return True
    return t.startswith(r if r.endswith(os.sep) else r + os.sep)


def _within(target: str, root: str) -> bool:
    """target 是否等于 root 或位于 root 之内（解析符号链接后比较）。"""
    return _under(_real(target), _real(root))


def _whitelisted(ap: str) -> bool:
    target = _real(ap)
    return any(_under(target, _real(w)) for w in _READ_WHITELIST)


# ---------------------------------------------------------------- 路径沙箱

class SandboxError(PermissionError):
    """路径越界。"""


def _protected(ap: str) -> bool:
    """是否命中工作区内的系统保护区（.memory 记忆目录）。

    延迟导入避免 sandbox ↔ memory 模块循环；memory 自身不依赖 sandbox 运行期状态。
    """
    from .memory import is_memory_path
    return is_memory_path(ap, workspace())


def safe_path(p: str) -> str:
    """写通道：把相对路径解析到工作区内，并阻止一切越界访问（读/写都拦）。

    这是所有「会修改文件系统」的工具必须走的入口；读类工具请改用 read_path，
    后者会按沙箱读取策略有条件放开沙箱外只读访问。
    工作区内的系统保护区（.memory）在两个通道里都不可访问 —— 记忆只能由
    core.memory 模块自己读写。
    """
    ws = workspace()
    raw = p if os.path.isabs(p) else os.path.join(ws, p)
    ap = os.path.abspath(raw)
    if not _within(ap, ws):
        raise SandboxError(f"路径越界：{ap} 不在工作区 {ws} 内")
    if _protected(ap):
        raise PermissionError(f".memory 是系统记忆目录，禁止工具访问：{ap}")
    return ap


def read_path(p: str) -> str:
    """读通道：相对路径锚定工作区；沙箱外绝对路径按读取策略判定。

      · 工作区内：始终放行（.memory 保护区除外）；
      · 工作区外：strict 拦截 / whitelist 仅白名单 / open 全部只读放开。
    返回解析后的绝对路径；越界时抛 SandboxError。
    """
    ws = workspace()
    raw = p if os.path.isabs(p) else os.path.join(ws, p)
    ap = os.path.abspath(raw)
    if _within(ap, ws):
        if _protected(ap):
            raise PermissionError(f".memory 是系统记忆目录，禁止工具访问：{ap}")
        return ap
    if _READ_MODE == "open":
        return ap
    if _READ_MODE == "whitelist" and _whitelisted(ap):
        return ap
    if _READ_MODE == "whitelist":
        raise SandboxError(
            f"路径越界：{ap} 不在工作区 {ws} 或只读白名单内"
        )
    raise SandboxError(
        f"路径越界：{ap} 不在工作区 {ws} 内"
        f"（沙箱外默认只读关闭，可在「通用配置」放开白名单/只读模式）"
    )


def assert_can_write(p: str) -> str:
    """写护栏的统一判定：写操作（含删除/移动/重命名）的目标必须在工作区内。

    给 run_python 的同进程护栏与命令检测复用 —— 与 safe_path 的区别只是
    错误信息面向「脚本越权」场景。返回规范化绝对路径。
    """
    ap = os.path.abspath(os.path.expanduser(p))
    ws = workspace()
    if not _within(ap, ws):
        raise PermissionError(
            f"沙箱外路径只读，禁止写入/删除：{ap}（工作区 {ws}）"
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


#: UTF-16 字节序标记（LE / BE）—— 用于把「夹着 NUL 的文本」和真二进制区分开。
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def looks_binary(data: bytes) -> bool:
    """含 NUL 字节基本就判定为二进制；带 UTF-16 BOM 的文本除外（NUL 是编码产物）。"""
    if data.startswith(_UTF16_BOMS):
        return False
    return b"\x00" in data[:8192]


def decode_bytes(data: bytes) -> tuple[str, str]:
    """按 UTF-8 → UTF-16(BOM) → GBK → Latin-1 顺序尝试解码，返回 (文本, 实际编码)。

    Windows 上不少老工具（配置生成器、记事本「Unicode」）会写出 UTF-16 LE + BOM，
    这类文件每个字符都夹着 0x00，如果不先按 BOM 识别，会被当成二进制而无法预览。
    """
    encodings = ["utf-8-sig"]
    if data.startswith(_UTF16_BOMS):
        encodings.append("utf-16")
    encodings += ["gbk", "latin-1"]
    for enc in encodings:
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
                f"确认要执行请设置环境变量 MINI_AGENT_ALLOW_DANGEROUS=1 后重试。"
            )
    return None


# ---------------------------------------------------------------- shell 越界写检测

# 整条语句里「所有路径参数都必须在工作区内」的写动词（含删除/移动）
_SHELL_WRITE_ALL = ("del", "erase", "rmdir", "rd", "move", "ren", "rename",
                    "mkdir", "md", "mklink", "rm", "cacls", "icacls", "takeown")
# 多源一目标：只强制最后一个路径（目标）在工作区内
_SHELL_WRITE_DST = ("copy", "xcopy", "robocopy", "cp", "mv")

_PATH_TOKEN = re.compile(r'^([A-Za-z]:[\\/]|\\\\)')  # 盘符绝对路径 / UNC
# 重定向目标：允许引号包裹（路径可含空格），也允许 `echo x>/etc/passwd` 这种紧贴写法
_REDIRECT = re.compile(r'>{1,2}\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s&|<>"\' ]+))')
# 语句分隔：&& || & | ; 与换行（原来只拆 & 系列，`;` 后的写动词整个被漏检）
_STMT_SEP = re.compile(r"&&|\|\||[&|;\r\n]")
# Windows 单字母开关（/F、/s、-f）：只在 Windows 下当开关，POSIX 的 /etc 是绝对路径
_WIN_SWITCH = re.compile(r"^[/-][A-Za-z]$")


def _strip_env_prefix(s: str) -> str:
    """剥掉命令前的环境变量赋值（`FOO=bar rm -rf X` → `rm -rf X`）。

    原来按 `=` 取首 token 会把动词认成 FOO，整句写操作检查直接跳过。
    """
    while True:
        m = re.match(r"^\s*[\w.]+=[^\s]*\s+", s)
        if not m:
            return s
        s = s[m.end():]


def _clean_token(token: str) -> str:
    """去掉引号与空白；`--output=PATH` 只保留 PATH，纯开关返回空串。"""
    t = str(token).strip().strip('"').strip("'")
    if not t:
        return ""
    if t.startswith("-"):
        if "=" not in t:
            return ""
        t = t.split("=", 1)[1].strip().strip('"').strip("'")
    return t


def _looks_like_path(t: str) -> bool:
    """是否可能是文件系统路径：绝对路径 / 盘符 / UNC / 含 ..。

    普通相对名（`normal.txt`）锚定在 cwd=工作区，不可能越界，不算路径候选。
    """
    if not t:
        return False
    if os.name == "nt" and _WIN_SWITCH.match(t):
        return False
    return os.path.isabs(t) or bool(_PATH_TOKEN.match(t)) or ".." in t


def _abs_token(t: str) -> str:
    """token → 绝对路径：相对锚定工作区，并展开 ~ / 环境变量（$HOME、%USERPROFILE%）。"""
    raw = t if (os.path.isabs(t) or _PATH_TOKEN.match(t)) else os.path.join(workspace(), t)
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))


def _token_escapes(token: str) -> bool:
    """路径 token 是否写到工作区外。"""
    t = _clean_token(token)
    if not _looks_like_path(t):
        return False
    return not _within(_abs_token(t), workspace())


def _token_hits_memory(token: str) -> bool:
    """路径 token 是否指向工作区的 .memory 保护区（相对/绝对都查）。"""
    t = _clean_token(token)
    if not t:
        return False
    from .memory import is_memory_path
    return is_memory_path(_abs_token(t), workspace())


def check_command_write(command: str) -> str | None:
    """检测 shell 命令里「写/删/移动到工作区外」或「触碰 .memory 保护区」的明显写法。

    覆盖：输出重定向（> / >>）与常见写动词（copy/move/del/rd/mkdir/...）。
    拦不住 powershell -c、程序内部写文件等混淆形式 —— 与 run_python 护栏
    同属「提高随手绕过成本」，不是强隔离边界。
    """
    # 1) 重定向目标（2>&1 这类目标是 &1，正则本身就不会把它当路径）
    for m in _REDIRECT.finditer(command):
        tok = m.group(1) or m.group(2) or m.group(3) or ""
        if tok in ("&1", "&2", "NUL", "nul", "/dev/null"):
            continue
        if _token_escapes(tok):
            return f"检测到重定向写入工作区外路径：{tok}（沙箱外只读，请改写到工作区内）"
        if _token_hits_memory(tok):
            return f"检测到重定向写入系统记忆目录：{tok}（.memory 受保护，禁止工具访问）"

    # 2) 按语句分隔符拆开，逐句看句首写动词
    for stmt in _STMT_SEP.split(command):
        s = _strip_env_prefix(stmt.strip())
        if not s:
            continue
        head_raw = re.split(r"[\s=]", s, 1)[0].strip('"').lower()
        head = os.path.basename(head_raw)  # 兼容 C:\Windows\System32\xcopy.exe 形态
        head = os.path.splitext(head)[0]
        if head not in _SHELL_WRITE_ALL and head not in _SHELL_WRITE_DST:
            continue
        # 粗分词：去引号，只保留像路径的 token（绝对路径 / 含 .. / .memory）
        tokens = [t.strip('"').strip("'") for t in re.split(r"\s+", s)[1:] if t.strip()]
        path_like = [t for t in tokens if _looks_like_path(_clean_token(t))]
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
