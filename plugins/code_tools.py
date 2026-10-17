"""
代码辅助工具组。

py_syntax_check  检查 Python 语法（不用真的跑，AST 解析即可）
count_code_lines  按目录统计代码行数（按扩展名分组，自动跳过空行/注释）
re_replace        用正则替换文件里的内容（批量改代码时很好用）
edit_file         精确块替换：old_string 必须唯一匹配，改代码比正则更安全
search_symbol     多语言符号搜索（函数/类/接口等定义，返回 文件:行号）
"""

from __future__ import annotations

import ast
import os
import re
import sys

from core import decode_bytes, looks_binary, read_bytes, read_path, safe_path, tool, workspace

PLUGIN_INFO = {"name": "代码辅助", "version": "1.0", "author": "mini-agent"}


@tool(
    "py_syntax_check",
    "检查 Python 文件的语法是否合法，返回错误行号或「语法 OK」。不执行代码。",
    params={"path": {"type": "string", "description": "Python 文件路径"}},
    required=["path"],
)
def py_syntax_check(path: str) -> str:
    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    if looks_binary(read_bytes(ap, 4096)):
        return f"[error] {ap} 不是文本文件"
    text, enc = decode_bytes(read_bytes(ap))
    try:
        ast.parse(text, filename=ap)
    except SyntaxError as e:
        return f"[syntax error] {ap}\n  行 {e.lineno} 列 {e.offset}: {e.msg}"
    return f"[ok] {ap} 语法正确（编码={enc}，{len(text.splitlines())} 行）"


@tool(
    "count_code_lines",
    "统计目录下各语言的代码行数（自动跳过空行和注释）。",
    params={
        "path": {"type": "string", "description": "目录路径，留空统计工作区", "default": "."},
        "max_files": {"type": "integer", "description": "最多扫描文件数", "default": 2000},
    },
)
def count_code_lines(path: str = ".", max_files: int = 2000) -> str:
    import glob as _glob

    ap = read_path(path)
    if not os.path.isdir(ap):
        return f"[error] 目录不存在：{ap}"

    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".java": "Java", ".go": "Go", ".rs": "Rust", ".c": "C", ".cpp": "C++",
        ".h": "C/C++ Header", ".html": "HTML", ".css": "CSS", ".md": "Markdown",
        ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
        ".sh": "Shell", ".bat": "Batch", ".ps1": "PowerShell",
    }

    stats: dict[str, dict[str, int]] = {}
    scanned = 0
    for fp in _glob.glob(os.path.join(ap, "**", "*"), recursive=True):
        if scanned >= max_files:
            break
        if not os.path.isfile(fp):
            continue
        # 防符号链接把扫描带出允许范围
        try:
            read_path(os.path.realpath(fp))
        except Exception:
            continue
        ext = os.path.splitext(fp)[1].lower()
        lang = ext_map.get(ext)
        if lang is None:
            continue
        if looks_binary(read_bytes(fp, 4096)):
            continue
        scanned += 1
        text, _ = decode_bytes(read_bytes(fp))
        total = len(text.splitlines())
        blank = sum(1 for l in text.splitlines() if not l.strip())
        comment = sum(
            1 for l in text.splitlines()
            if l.strip().startswith(("#", "//", "/*", "*", "--", ";"))
        )
        code = total - blank - comment
        s = stats.setdefault(lang, {"files": 0, "total": 0, "code": 0, "blank": 0, "comment": 0})
        s["files"] += 1
        s["total"] += total
        s["code"] += max(0, code)
        s["blank"] += blank
        s["comment"] += comment

    if not stats:
        return f"[{ap}] 没有找到可统计的代码文件"

    rows = sorted(stats.items(), key=lambda kv: kv[1]["code"], reverse=True)
    lines = [f"[{ap}] 扫描 {scanned} 个文件\n", "语言        文件数  总行  代码  空行  注释"]
    for lang, s in rows:
        lines.append(
            f"{lang:10s} {s['files']:5d} {s['total']:6d} {s['code']:6d} {s['blank']:5d} {s['comment']:6d}"
        )
    total_code = sum(s["code"] for s in stats.values())
    lines.append(f"\n代码行合计: {total_code:,}")
    return "\n".join(lines)


@tool(
    "re_replace",
    "用正则表达式替换文件里的内容。返回替换了多少处，以及前几处的前后对比。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "pattern": {"type": "string", "description": "要查找的正则表达式"},
        "repl": {"type": "string", "description": "替换内容（支持 \\1 等反向引用）"},
        "count": {"type": "integer", "description": "最多替换处数，0 表示全部", "default": 0},
    },
    required=["path", "pattern", "repl"],
    dangerous=True,
)
def re_replace(path: str, pattern: str, repl: str, count: int = 0) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[error] 正则不合法：{e}"

    data = read_bytes(ap)
    if looks_binary(data):
        return f"[error] 不支持对二进制文件做文本替换：{ap}"
    text, enc = decode_bytes(data)

    # 先预览前 3 处
    previews = []
    for m in rx.finditer(text):
        if len(previews) >= 3:
            break
        start = max(0, m.start() - 20)
        end = min(len(text), m.end() + 20)
        original = text[start:end].replace("\n", " ")
        replaced = rx.sub(repl, text[start:end], count=1)
        previews.append(f"  - {original!r} → {replaced!r}")

    new_text, n = rx.subn(repl, text, count=count if count else 0)
    if n == 0:
        return f"[ok] 没有匹配到 {pattern!r}，文件未改动"

    with open(ap, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    preview = "\n" + "\n".join(previews) if previews else ""
    return f"[ok] {ap} 替换 {n} 处（编码={enc}）{preview}"


@tool(
    "edit_file",
    "精确替换文件中的一段文本（适合改代码，比正则安全）：old_string 必须与文件内容"
    "完全一致且唯一匹配；匹配 0 处或多处都会拒绝，多处时可显式开 replace_all。"
    "建议 old_string 多带几行上下文以保证唯一。",
    params={
        "path": {"type": "string", "description": "文件路径"},
        "old_string": {"type": "string", "description": "要被替换的原文（含精确缩进）"},
        "new_string": {"type": "string", "description": "替换后的文本"},
        "replace_all": {
            "type": "boolean",
            "description": "匹配多处时是否全部替换（默认 false：多处即拒绝，防误改）",
            "default": False,
        },
    },
    required=["path", "old_string", "new_string"],
    dangerous=True,
)
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    ap = safe_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"
    if not old_string:
        return "[error] old_string 不能为空"

    data = read_bytes(ap)
    if looks_binary(data):
        return f"[error] 不支持对二进制文件做文本编辑：{ap}"
    text, enc = decode_bytes(data)

    if old_string == new_string:
        return "[ok] old_string 与 new_string 相同，文件未改动"

    # 行尾适配：read_file 输出不带行尾，模型构造的 old_string 一律是 \n，
    # 而磁盘文件可能是 CRLF（Windows 常见）。先按原文试，不中再按文件行尾适配，
    # 命中后只替换目标片段，文件其余字节（含行尾风格）保持不变。
    crlf = "\r\n" in text

    def _adapt_eol(s: str) -> str:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        return s.replace("\n", "\r\n") if crlf else s

    old_try = old_string if old_string in text else _adapt_eol(old_string)
    new_try = _adapt_eol(new_string)

    # 找出全部匹配位置及其行号
    hits: list[int] = []
    start = 0
    while True:
        i = text.find(old_try, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1
    line_of = lambda pos: text.count("\n", 0, pos) + 1

    if not hits:
        return (
            f"[error] 未找到匹配，文件未改动。请检查 old_string 是否与文件完全一致"
            f"（缩进/空行/引号都要一致），可先用 read_file 查看原文。"
        )
    if len(hits) > 1 and not replace_all:
        lines = ", ".join(str(line_of(p)) for p in hits[:10])
        more = " …" if len(hits) > 10 else ""
        return (
            f"[error] 匹配到 {len(hits)} 处（行 {lines}{more}），拒绝替换以防误改。"
            f"请扩大 old_string 的上下文使其唯一，或确认要全改时传 replace_all=true。"
        )

    if replace_all:
        new_text = text.replace(old_try, new_try)
        changed = len(hits)
    else:
        pos = hits[0]
        new_text = text[:pos] + new_try + text[pos + len(old_try):]
        changed = 1

    # 原文件是带 BOM 的 UTF-8 时保留 BOM，避免引入无关 diff
    with open(ap, "w", encoding="utf-8-sig" if enc == "utf-8-sig" else "utf-8", newline="") as f:
        f.write(new_text)

    first_line = line_of(hits[0])
    snippet = old_string.strip().splitlines()[0][:80] if old_string.strip() else ""
    where = f"行 {first_line}" if changed == 1 else f"{changed} 处，首处行 {first_line}"
    return f"[ok] {ap} 已替换（{where}，编码={enc}）\n  - {snippet}"


# ------------------------------------------------------------- search_symbol

# 各语言的定义模式：(正则, 种类)，正则的第 1 个捕获组必须是符号名
_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".idea", ".vscode", ".next", ".nuxt", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "coverage", "target", "out", "bin", "obj",
    ".gradle", ".parcel-cache", "vendor",
}

_SYMBOL_RULES: dict[tuple[str, ...], list[tuple[str, str]]] = {
    (".py",): [
        (r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", "fn"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
    ],
    (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue"): [
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "fn"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
         r"(?:\([^)]*\)|[A-Za-z_$]\w*)\s*=>", "fn"),
        (r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
        (r"^\s*(?:export\s+)?(?:interface|enum|type)\s+([A-Za-z_$][\w$]*)", "class"),
    ],
    (".go",): [
        (r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", "fn"),
        (r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)", "class"),
    ],
    (".java", ".kt", ".kts"): [
        (r"^\s*(?:public|private|protected|internal|override|suspend|abstract|open|final|"
         r"static|\s)*fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)", "fn"),
        (r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|\s)+"
         r"[\w<>\[\],.?!\s]+?\s+([A-Za-z_]\w*)\s*\([^;)]*\)\s*(?:throws[^{]*)?[{]?\s*$", "fn"),
        (r"^\s*(?:public|private|protected|static|final|abstract|data|sealed|\s)*"
         r"(?:class|interface|enum|object)\s+([A-Za-z_]\w*)", "class"),
    ],
    (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cs"): [
        (r"^\s*(?:public|private|protected|internal|static|async|override|virtual|abstract|"
         r"sealed|extern|inline|\s)*[\w:<>\[\],.?!\s]+?\s+([A-Za-z_]\w*)\s*"
         r"\([^;)]*\)\s*(?:const|noexcept|override|final|\s)*[{]?\s*$", "fn"),
        (r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct|enum|union|interface)\s+"
         r"([A-Za-z_]\w*)", "class"),
    ],
    (".rs",): [
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+"
         r"([A-Za-z_]\w*)", "fn"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)", "class"),
    ],
    (".rb",): [
        (r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)", "fn"),
        (r"^\s*(?:class|module)\s+([A-Za-z_]\w*)", "class"),
    ],
    (".php",): [
        (r"^\s*(?:public|private|protected|static|abstract|final|\s)*function\s+&?"
         r"([A-Za-z_]\w*)", "fn"),
        (r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)",
         "class"),
    ],
    (".sh", ".bash", ".zsh", ".bats"): [
        (r"^\s*function\s+([A-Za-z_]\w*)\s*(?:\(\s*\))?\s*\{?", "fn"),
        (r"^\s*([A-Za-z_]\w*)\s*\(\s*\)\s*\{?", "fn"),
    ],
    (".lua",): [
        (r"^\s*(?:local\s+)?function\s+(?:[A-Za-z_]\w*[:.])*([A-Za-z_]\w*)", "fn"),
        (r"^\s*(?:local\s+)?([A-Za-z_]\w*)\s*=\s*function", "fn"),
    ],
    (".swift",): [
        (r"^\s*(?:public|private|internal|fileprivate|open|static|class|override|mutating|\s)*"
         r"func\s+([A-Za-z_]\w*)", "fn"),
        (r"^\s*(?:public|private|internal|fileprivate|open|static|final|\s)*"
         r"(?:class|struct|enum|protocol|actor)\s+([A-Za-z_]\w*)", "class"),
    ],
}

# 展平成 扩展名 → 已编译模式列表，并排除明显不是函数定义的控制关键字
_EXT_RULES: dict[str, list[tuple[re.Pattern, str]]] = {}
for exts, rules in _SYMBOL_RULES.items():
    compiled = [(re.compile(pat), kind) for pat, kind in rules]
    for ext in exts:
        _EXT_RULES[ext] = compiled
_CONTROL_WORDS = {"if", "for", "while", "switch", "catch", "return", "else", "do"}


@tool(
    "search_symbol",
    "按名字在代码库里搜索符号定义（函数/方法、类/接口/枚举等），返回「文件:行号:类型:签名」。"
    "支持 Python/JS/TS/Go/Java/Kotlin/C/C++/C#/Rust/Ruby/PHP/Shell/Lua/Swift。"
    "只找定义处，不找调用处；自动跳过 node_modules/.git/dist 等目录。",
    params={
        "name": {"type": "string", "description": "符号名（默认大小写不敏感的子串匹配）"},
        "path": {"type": "string", "description": "搜索根目录", "default": "."},
        "kind": {
            "type": "string",
            "description": "符号类型：all（默认）/ fn（函数方法）/ class（类接口枚举等）",
            "default": "all",
        },
        "exact": {"type": "boolean", "description": "true 表示按符号名精确匹配", "default": False},
        "max_results": {"type": "integer", "description": "结果上限", "default": 200},
    },
    required=["name"],
)
def search_symbol(
    name: str, path: str = ".", kind: str = "all", exact: bool = False, max_results: int = 200
) -> str:
    ap = read_path(path)
    if not os.path.isdir(ap):
        return f"[error] 目录不存在：{ap}"
    kind = (kind or "all").lower()
    if kind not in ("all", "fn", "class"):
        return "[error] kind 只能是 all / fn / class"
    needle = name.lower()

    root = workspace()
    results: list[tuple[str, int, str, str]] = []  # (相对路径, 行号, 种类, 签名)
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(ap):
        # 就地剪枝：不进入依赖/产物/隐藏目录（保留 . 开头但非黑名单的目录）
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if len(results) >= max_results:
                break
            rules = _EXT_RULES.get(os.path.splitext(fn)[1].lower())
            if not rules:
                continue
            fp = os.path.join(dirpath, fn)
            # 防符号链接把读取带出允许范围
            try:
                read_path(os.path.realpath(fp))
            except Exception:
                continue
            try:
                data = read_bytes(fp, 2 * 1024 * 1024)
            except OSError:
                continue
            if looks_binary(data):
                continue
            scanned += 1
            text, _ = decode_bytes(data)
            for i, line in enumerate(text.splitlines(), 1):
                for rx, k in rules:
                    if kind != "all" and k != kind:
                        continue
                    m = rx.match(line)
                    if not m:
                        continue
                    sym = m.group(1)
                    if sym in _CONTROL_WORDS:
                        continue
                    hit = sym.lower() == needle if exact else needle in sym.lower()
                    if not hit:
                        continue
                    sig = line.strip()[:160]
                    rel = os.path.relpath(fp, root)
                    results.append((rel if not rel.startswith("..") else os.path.abspath(fp), i, k, sig))
                    break  # 同一行只取第一个命中
        if len(results) >= max_results:
            break

    if not results:
        return f"[ok] 扫描 {scanned} 个文件，没有找到符号「{name}」"

    # 按文件分组输出
    lines = [f"[ok] 扫描 {scanned} 个文件，命中 {len(results)} 个符号定义（上限 {max_results}）"]
    cur_file = None
    for rel, ln, k, sig in results:
        if rel != cur_file:
            cur_file = rel
            lines.append(rel)
        lines.append(f"  {ln:6d}  {k:5s}  {sig}")
    return "\n".join(lines)
