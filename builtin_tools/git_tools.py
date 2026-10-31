"""
Git 工具组。

所有命令都通过 run_process 执行，并经过 check_command 白名单校验。
只读操作（status/diff/log）非危险；分支切换、提交标 dangerous。
"""

from __future__ import annotations

from core import check_command, run_process, safe_path, tool, workspace

TOOL_INFO = {"name": "Git", "version": "1.0", "author": "mini-agent"}


def _git(args: str, cwd: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    cmd = check_command(f"git {args}")
    code, out, err, _ = run_process(cmd, shell=True, cwd=cwd or workspace(), timeout=timeout)
    return code, out, err


@tool(
    "git_status",
    "查看 git 工作区状态（哪些文件已修改/新增/删除）。",
    params={"path": {"type": "string", "description": "Git 仓库目录，留空用工作区", "default": "."}},
)
def git_status(path: str = ".") -> str:
    ap = safe_path(path)
    code, out, err = _git("status --short", cwd=ap)
    if code != 0:
        return f"[error] {err or out}"
    return out or "工作区干净（无未提交改动）"


@tool(
    "git_diff",
    "查看未提交的改动内容。可指定某个文件。",
    params={
        "path": {"type": "string", "description": "Git 仓库目录", "default": "."},
        "file": {"type": "string", "description": "只看某个文件的 diff，留空看全部", "default": ""},
        "staged": {"type": "boolean", "description": "True=看已暂存的改动", "default": False},
    },
)
def git_diff(path: str = ".", file: str = "", staged: bool = False) -> str:
    ap = safe_path(path)
    args = "diff --cached" if staged else "diff"
    if file:
        args += f" -- {file}"
    code, out, err = _git(args, cwd=ap)
    if code != 0:
        return f"[error] {err or out}"
    return out or "（没有改动）"


@tool(
    "git_log",
    "查看最近的提交历史。",
    params={
        "path": {"type": "string", "description": "Git 仓库目录", "default": "."},
        "count": {"type": "integer", "description": "显示最近 N 条", "default": 10},
    },
)
def git_log(path: str = ".", count: int = 10) -> str:
    ap = safe_path(path)
    code, out, err = _git(
        f'log -{count} --pretty=format:"%h %ad %an | %s" --date=short', cwd=ap
    )
    if code != 0:
        return f"[error] {err or out}"
    return out or "（没有提交记录）"


@tool(
    "git_branch",
    "列出分支或切换分支。",
    params={
        "path": {"type": "string", "description": "Git 仓库目录", "default": "."},
        "checkout": {"type": "string", "description": "要切换到的分支名，留空只列分支", "default": ""},
        "create": {"type": "string", "description": "新建并切换到分支，留空则不新建", "default": ""},
    },
    dangerous=True,
)
def git_branch(path: str = ".", checkout: str = "", create: str = "") -> str:
    ap = safe_path(path)
    if create:
        code, out, err = _git(f"checkout -b {create}", cwd=ap)
        if code != 0:
            return f"[error] {err or out}"
        return f"[ok] 已创建并切换到分支 {create}\n{out}"
    if checkout:
        code, out, err = _git(f"checkout {checkout}", cwd=ap)
        if code != 0:
            return f"[error] {err or out}"
        return f"[ok] 已切换到分支 {checkout}\n{out}"
    # 只列分支
    code, out, err = _git("branch --list", cwd=ap)
    if code != 0:
        return f"[error] {err or out}"
    return out or "（没有分支）"


@tool(
    "git_commit",
    "把所有改动提交到 git。",
    params={
        "path": {"type": "string", "description": "Git 仓库目录", "default": "."},
        "message": {"type": "string", "description": "提交说明"},
        "add_all": {"type": "boolean", "description": "是否先 git add -A 把所有改动加入暂存区", "default": True},
    },
    required=["message"],
    dangerous=True,
)
def git_commit(path: str = ".", message: str = "", add_all: bool = True) -> str:
    ap = safe_path(path)
    if add_all:
        code, _, err = _git("add -A", cwd=ap)
        if code != 0:
            return f"[error] git add 失败：{err}"
    code, out, err = _git(f'commit -m "{message}"', cwd=ap)
    if code != 0:
        return f"[error] git commit 失败：{err or out}"
    return f"[ok] 已提交\n{out}"
