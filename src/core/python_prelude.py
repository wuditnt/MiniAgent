"""
run_python 的写护栏（prelude）。

背景：
    run_python 是在子进程里跑用户/模型给的任意 Python，插件层的 safe_path()
    管不到它 —— 脚本里一句 open(r"C:\\...", "w") 就能绕过工作区沙箱。
    因此每次执行前，在用户代码前面拼一段 prelude：它在子进程内 monkey-patch
    open / os / shutil / pathlib / sqlite3 的写入口，统一用内联的工作区常量
    判定，沙箱外的写入/删除/移动直接抛 PermissionError。

边界（已知，无法在同进程内 100% 封堵）：
    · ctypes / C 扩展 / 直接 import _io 拿原函数等绕过手法拦不住；
    · 真正不可信的代码需要容器/低权限账号级隔离。
    本护栏的目标是消除「普通脚本随手就能写出去」的无意/低成本旁路。

prelude 必须自包含：子进程虽与父进程同环境（PYTHONPATH 含 src），
但不依赖 core 包；工作区根在生成代码时以字符串字面量内联，避免受
子进程环境变量 / cwd 影响。整段包在一次性函数里执行，patch 留在模块
对象上，而 _ma_* 私有名不污染用户脚本的全局命名空间。
"""

from __future__ import annotations

import os
import textwrap

# 护栏函数体。_BODY 是原始字符串、不经过 format —— 里面的 f-string
# 大括号照常写单括号；工作区路径用 __MA_WORKSPACE__ 占位符替换。
_BODY = r'''
def _install():
    import builtins as _builtins
    import io as _io
    import os as _os
    import shutil as _shutil
    import pathlib as _pathlib

    _WS = __MA_WORKSPACE__
    _PROT = __MA_MEMORY__

    def _norm(p):
        return _os.path.normcase(_os.path.abspath(_os.path.expanduser(_os.fspath(p))))

    def _within(p):
        ap = _norm(p)
        root = _os.path.normcase(_os.path.abspath(_WS))
        return ap == root or ap.startswith(root + _os.sep)

    def _protected(p):
        ap = _norm(p)
        pp = _os.path.normcase(_os.path.abspath(_PROT))
        return ap == pp or ap.startswith(pp + _os.sep)

    def _guard(p, what):
        if _protected(p):
            raise PermissionError(
                f".memory 是系统记忆目录，禁止{what}：{_os.path.abspath(_os.fspath(p))}"
            )
        if not _within(p):
            raise PermissionError(
                f"沙箱外路径只读，禁止{what}：{_os.path.abspath(_os.fspath(p))}"
                f"（工作区 {_os.path.abspath(_WS)}）"
            )

    # ---------- open()：按 mode 判定 ----------
    _open_orig = _builtins.open

    def _open(file, mode="r", *args, **kwargs):
        if not isinstance(file, int):  # 文件描述符无法判路径，放行
            m = str(mode).lower()
            if any(c in m for c in ("w", "a", "x", "+")):
                _guard(file, "写入")
            elif _protected(file):
                raise PermissionError(
                    f".memory 是系统记忆目录，禁止读取：{_os.path.abspath(_os.fspath(file))}"
                )
        return _open_orig(file, mode, *args, **kwargs)

    _builtins.open = _open
    _io.open = _open  # pathlib.Path.open 内部走 io.open

    # ---------- os：删除 / 创建 / 重命名类 ----------
    def _wrap1(name, what):
        orig = getattr(_os, name, None)
        if orig is None:
            return

        def wrapper(p, *a, **k):
            _guard(p, what)
            return orig(p, *a, **k)

        setattr(_os, name, wrapper)

    def _wrap2(name, what):
        orig = getattr(_os, name, None)
        if orig is None:
            return

        def wrapper(src, dst, *a, **k):
            _guard(src, what)
            _guard(dst, what)
            return orig(src, dst, *a, **k)

        setattr(_os, name, wrapper)

    _wrap1("remove", "删除")
    _wrap1("unlink", "删除")
    _wrap1("rmdir", "删除")
    _wrap1("mkdir", "创建目录")
    _wrap1("makedirs", "创建目录")
    _wrap2("rename", "移动/重命名")
    _wrap2("replace", "移动/替换")

    _link_orig = getattr(_os, "link", None)
    if _link_orig is not None:
        def _link(src, dst, *a, **k):
            _guard(dst, "创建链接")
            return _link_orig(src, dst, *a, **k)
        _os.link = _link

    _symlink_orig = getattr(_os, "symlink", None)
    if _symlink_orig is not None:
        def _symlink(src, dst, *a, **k):
            _guard(dst, "创建符号链接")
            return _symlink_orig(src, dst, *a, **k)
        _os.symlink = _symlink

    # ---------- shutil ----------
    def _rmtree(path, *a, **k):
        _guard(path, "递归删除")
        return _rmtree_orig(path, *a, **k)
    _rmtree_orig = _shutil.rmtree
    _shutil.rmtree = _rmtree

    def _move(src, dst, *a, **k):
        _guard(src, "移动")
        _guard(dst, "移动")
        return _move_orig(src, dst, *a, **k)
    _move_orig = _shutil.move
    _shutil.move = _move

    for _n in ("copyfile", "copy", "copy2"):
        if hasattr(_shutil, _n):
            def _make(n=_n):
                orig = getattr(_shutil, n)

                def wrapper(src, dst, *a, **k):
                    if _protected(src):
                        raise PermissionError(
                            f".memory 是系统记忆目录，禁止读取：{_os.path.abspath(_os.fspath(src))}"
                        )
                    _guard(dst, "写入")  # 源是读取，可来自区外；目标必须区内
                    return orig(src, dst, *a, **k)
                setattr(_shutil, n, wrapper)
            _make()

    def _copytree(src, dst, *a, **k):
        if _protected(src):
            raise PermissionError(
                f".memory 是系统记忆目录，禁止读取：{_os.path.abspath(_os.fspath(src))}"
            )
        _guard(dst, "写入")
        return _copytree_orig(src, dst, *a, **k)
    _copytree_orig = _shutil.copytree
    _shutil.copytree = _copytree

    # ---------- pathlib.Path ----------
    _P = _pathlib.Path

    def _p1(name, what):
        orig = getattr(_P, name, None)
        if orig is None:
            return

        def wrapper(self, *a, **k):
            _guard(self, what)
            return orig(self, *a, **k)
        setattr(_P, name, wrapper)

    def _p2(name, what):
        orig = getattr(_P, name, None)
        if orig is None:
            return

        def wrapper(self, target, *a, **k):
            _guard(self, what)
            _guard(target, what)
            return orig(self, target, *a, **k)
        setattr(_P, name, wrapper)

    _p1("write_text", "写入")
    _p1("write_bytes", "写入")
    _p1("unlink", "删除")
    _p1("mkdir", "创建目录")
    _p1("touch", "写入")
    _p2("rename", "移动/重命名")
    _p2("replace", "移动/替换")
    _p1("symlink_to", "创建符号链接")
    _p1("hardlink_to", "创建硬链接")
    _p1("link_to", "创建硬链接")  # 旧版本 Python

    # ---------- sqlite3：区外库禁止读写连接（只读 URI 放行）----------
    try:
        import sqlite3 as _sqlite3

        _connect_orig = _sqlite3.connect

        def _connect(database, *a, **k):
            if isinstance(database, str) and database not in (":memory:", ""):
                uri = database if k.get("uri") else None
                readonly = bool(uri) and "mode=ro" in uri
                # .memory 内的库任何连接方式都拦（只读也会泄露记忆内容）
                if uri and database.startswith("file:"):
                    from urllib.request import url2pathname
                    _db_path = url2pathname(database[5:].split("?", 1)[0])
                else:
                    _db_path = database
                if _protected(_db_path):
                    raise PermissionError(
                        f".memory 是系统记忆目录，禁止访问数据库：{_os.path.abspath(_db_path)}"
                    )
                if not readonly:
                    _guard(_db_path, "写数据库")
            return _connect_orig(database, *a, **k)

        _sqlite3.connect = _connect
    except Exception:
        pass


_install()
'''


def build_prelude(workspace: str) -> str:
    """生成注入到用户代码之前的护栏源码。

    workspace 为当前工作区绝对路径，以 repr 字面量内联（r'' 原始串 +
    占位符替换，避免与函数体里的 f-string 双括号转义互相干扰）。
    .memory 保护区路径同样内联，子进程不依赖 core 包即可判定。
    """
    body = (
        _BODY
        .replace("__MA_WORKSPACE__", repr(workspace))
        .replace("__MA_MEMORY__", repr(os.path.join(workspace, ".memory")))
    )
    return (
        "# === mini-agent 沙箱写护栏（自动注入）===\n"
        + textwrap.dedent(body).strip()
        + "\ndel _install\n"
        + "# === 沙箱写护栏注入结束 ===\n"
    )
