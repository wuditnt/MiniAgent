"""
后端调用 Python 解释器的唯一入口 —— 一个环境变量决定「用哪个 Python」。

为什么需要这个模块：
    run_python 之前直接写 sys.executable，MCP 在 _resolve_command 里另行映射，
    而 run_shell 传的是字符串命令、完全依赖子进程 PATH。三处来源不一致的典型
    症状是：模型执行 `python -m pip install xxx` 装进了系统 Python，而后端在
    venv 里 import 不到。这里把「选哪个解释器」收敛成一个函数，并统一加工
    子进程环境（把解释器目录前置到 PATH）。

配置：
    MINI_AGENT_PYTHON —— 解释器绝对路径，写在项目根 `.env`（见 .env.example）。
    未配置时回退 sys.executable，即启动后端时的那个解释器，行为与以前一致。

约束：
  · 每次调用都读环境变量、不缓存 —— `.env` 的读取（main.load_dotenv）可能晚于
    本模块首次导入，缓存会把「还没读到配置」的状态固化下来；
  · 配置了但文件不存在时不抛异常，只记一次警告并回退 —— 路径写错不该让整个
    后端起不来，但也不能静默假装配置生效。
"""

from __future__ import annotations

import os
import sys

#: 环境变量名（写在项目根 .env）
ENV_VAR = "MINI_AGENT_PYTHON"

#: 已经警告过的非法取值，避免每次调用都刷日志
_warned: set[str] = set()


def _clean(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _warn_bad_path(raw: str) -> None:
    if raw in _warned:
        return
    _warned.add(raw)
    try:
        from .logging_setup import get_logger

        get_logger("app").warning(
            "python.executable_not_found env=%s value=%r（回退 sys.executable）", ENV_VAR, raw
        )
    except Exception:  # noqa: BLE001 —— 记日志失败不该影响工具执行
        pass


def executable() -> str:
    """当前应当使用的 Python 解释器绝对路径。

    优先 MINI_AGENT_PYTHON；未配置或指向不存在的文件时回退 sys.executable。
    """
    raw = _clean(os.environ.get(ENV_VAR, ""))
    if not raw:
        return sys.executable
    path = os.path.abspath(os.path.expanduser(raw))
    if os.path.isfile(path):
        return path
    _warn_bad_path(raw)
    return sys.executable


def bin_dir() -> str:
    """解释器所在目录（Windows 下即 `<venv>\\Scripts`）。"""
    return os.path.dirname(executable())


def _same_dir(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p)).rstrip("\\/")

    return norm(a) == norm(b)


def _venv_root(folder: str) -> str:
    """如果解释器目录属于虚拟环境，返回环境根目录；否则返回空串。

    判定依赖 pyvenv.cfg —— 只看目录名叫 Scripts/bin 会把系统 Python 误判成 venv。
    """
    name = os.path.basename(folder).lower()
    if (os.name == "nt" and name == "scripts") or (os.name != "nt" and name == "bin"):
        root = os.path.dirname(folder)
        if os.path.isfile(os.path.join(root, "pyvenv.cfg")):
            return root
    return ""


def subprocess_env(base: dict | None = None) -> dict:
    """加工一份给子进程用的环境变量。

    做两件事：
      · PATH 前置解释器所在目录 —— 让 `run_shell` 里写的 `python` / `pip`
        与后端使用同一个环境（venv 未 activate 时也能命中）；
      · 子进程 stdio 统一 UTF-8 —— 非中文代码页（英文 Windows 的 cp1252）上
        打印中文不会在 stdout.write 处抛 UnicodeEncodeError。
        只定 stdio 编码，不动子进程文件读写的默认编码。
    """
    env = dict(os.environ if base is None else base)
    folder = bin_dir()
    if folder:
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        env["PATH"] = os.pathsep.join([folder, *[p for p in parts if not _same_dir(p, folder)]])
        venv = _venv_root(folder)
        if venv:
            env.setdefault("VIRTUAL_ENV", venv)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
