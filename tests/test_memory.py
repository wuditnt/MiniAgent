"""
记忆系统测试。不需要 API Key，不花任何费用。

覆盖八处容易出错的地方：
  1. 存储布局 —— .memory/topics 自动创建 + Windows 隐藏属性
  2. 条目追加 —— 时间戳行格式、相似去重、单行压缩
  3. 主题摘要 —— 写入 / 读取 / 滚动裁剪到 TOPICS_KEEP
  4. 保护区 —— sandbox 双通道（safe_path / read_path）都拒 .memory
  5. 列举与搜索 —— list_dir / glob / grep / 代码统计都看不见 .memory
  6. run_python 护栏 —— open/pathlib/shutil 触碰 .memory 被拒，区内正常
  7. shell 检测 —— 重定向/删除/复制到 .memory 被拦
  8. 注入渲染 —— 三层拼接顺序、预算裁剪、空记忆不产生段落
  9. 记忆分级 —— 条目类型解析（含旧格式兼容）、注入排序、容量淘汰
 10. 提炼过滤 —— 非法/低价值类型在写入前被丢弃
 11. 检索与合并 —— 规则常驻 + 相关度 top-k、预算截断、同主题 upsert
 12. 用户显式指令 —— 8 种「请帮我记住」句式、反例、画像类型粗判
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

import core.sandbox as sandbox  # noqa: E402
from core import memory  # noqa: E402


def _new_ws() -> str:
    return tempfile.mkdtemp(prefix="ma-mem-")


# ============================================================ 1. 布局与隐藏

def test_layout_and_hidden():
    ws = _new_ws()
    try:
        md = memory.ensure_layout(ws)
        assert os.path.isdir(md), md
        assert os.path.isdir(os.path.join(md, "topics"))
        assert memory.project_path(ws) == os.path.join(md, "project.md")
        if os.name == "nt":
            import ctypes
            attr = ctypes.windll.kernel32.GetFileAttributesW(md)
            assert attr != -1 and (attr & 0x02), "目录未加隐藏属性"
        # 幂等
        assert memory.ensure_layout(ws) == md
        print("✓ 布局：.memory/topics 创建、路径正确、Windows 隐藏属性、幂等")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_gitignore():
    ws = _new_ws()
    try:
        # 非 git 仓库不动 .gitignore
        assert memory.ensure_gitignore(ws) is False
        assert not os.path.isfile(os.path.join(ws, ".gitignore"))
        os.makedirs(os.path.join(ws, ".git"), exist_ok=True)
        assert memory.ensure_gitignore(ws) is True
        content = open(os.path.join(ws, ".gitignore"), encoding="utf-8").read()
        assert ".memory/" in content
        # 幂等：已存在则不重复追加
        assert memory.ensure_gitignore(ws) is False
        assert content.count(".memory/") == 1
        print("✓ .gitignore：仅 git 仓库追加、幂等不重复")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ============================================================ 2. 条目追加

def test_append_and_dedup():
    ws = _new_ws()
    try:
        assert memory.append_project_note("后端改完必须杀 python 重启", workspace=ws)
        text = memory.load_project(ws)
        entries = memory._parse_entries(text)
        assert len(entries) == 1 and "杀 python 重启" in entries[0]["body"]
        assert entries[0]["source"] == "对话"
        assert len(entries[0]["ts"]) == 16   # YYYY-MM-DD HH:MM

        # 同主题的新表述 → upsert 替换（条数不变，内容更新为新表述）
        assert memory.append_project_note("后端改完必须杀 python 重启！", workspace=ws)
        entries = memory._parse_entries(memory.load_project(ws))
        assert len(entries) == 1 and entries[0]["body"].endswith("！"), entries

        # 完全相同的重复 → 跳过
        assert not memory.append_project_note("后端改完必须杀 python 重启！", workspace=ws)
        assert len(memory._parse_entries(memory.load_project(ws))) == 1

        # 明显不同则写入
        assert memory.append_project_note("前端用 npx.cmd 而不是 npx", workspace=ws)
        assert len(memory._parse_entries(memory.load_project(ws))) == 2

        # 多行内容压成单行，保证条目文件可按行解析
        assert memory.append_project_note("第一行\n第二行", workspace=ws)
        last = memory._parse_entries(memory.load_project(ws))[-1]
        assert last["body"] == "第一行 第二行"

        # 空内容不写
        assert not memory.append_project_note("   ", workspace=ws)
        print("✓ 条目：时间戳格式、近似去重、单行压缩、空内容忽略")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ============================================================ 3. 主题摘要

def test_topics_and_prune():
    ws = _new_ws()
    try:
        memory.save_topic("s1", "## 摘要一\n排查端口占用", ws)
        assert memory.load_topic("s1", ws) == "## 摘要一\n排查端口占用"
        # 覆盖式写入（滚动摘要每轮整体替换）
        memory.save_topic("s1", "## 摘要二\n合并后的新摘要", ws)
        assert "合并后的新摘要" in memory.load_topic("s1", ws)

        # 裁剪：超过 TOPICS_KEEP 只留最近的
        for i in range(memory.TOPICS_KEEP + 5):
            memory.save_topic(f"bulk{i}", f"topic {i}", ws)
        items = memory.list_topics(ws)
        assert len(items) == memory.TOPICS_KEEP, len(items)
        # 最新的在最前
        assert items[0]["session"] == f"bulk{memory.TOPICS_KEEP + 4}"

        # 删除
        memory.delete_topic("s1", ws)
        assert memory.load_topic("s1", ws) == ""
        assert all(it["session"] != "s1" for it in memory.list_topics(ws))

        # 会话 id 里的路径分隔符被清洗，不会逃出 topics 目录
        tp = memory.topic_path("../../etc/passwd", ws)
        assert os.path.dirname(tp) == os.path.join(memory.memory_dir(ws), "topics")
        print("✓ 主题摘要：覆盖写、保留最近 N 条、删除、会话 id 穿越防护")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ============================================================ 4. 沙箱保护区

def test_sandbox_guard():
    ws = _new_ws()
    old = sandbox.workspace()
    try:
        sandbox.set_workspace(ws)
        md = memory.ensure_layout(ws)
        targets = [
            ".memory",
            ".memory/project.md",
            os.path.join(md, "topics", "s1.md"),
            os.path.join(ws, ".memory", "x.txt"),
        ]
        for p in targets:
            for fn, name in ((sandbox.safe_path, "safe_path"), (sandbox.read_path, "read_path")):
                try:
                    fn(p)
                    raise AssertionError(f"{name} 未拦截 {p}")
                except PermissionError as e:
                    assert ".memory" in str(e), str(e)
        # 保护区外的正常路径不受影响
        assert sandbox.safe_path("a/b.txt") == os.path.join(ws, "a", "b.txt")
        assert sandbox.read_path("a/b.txt") == os.path.join(ws, "a", "b.txt")
        # assert_can_write 同规则
        try:
            sandbox.assert_can_write(os.path.join(md, "project.md"))
            raise AssertionError("assert_can_write 未拦截")
        except PermissionError:
            pass

        # --- 符号链接逃逸：路径归一必须解析链接，否则区内链接就是通用越界跳板 ---
        outside = tempfile.mkdtemp(prefix="ma-mem-out-")
        try:
            link = os.path.join(ws, "link_out")
            kw = {"target_is_directory": True} if os.name == "nt" else {}
            try:
                os.symlink(outside, link, **kw)
            except OSError:
                print("· 符号链接用例跳过（当前环境无创建链接权限）")
            else:
                for fn, name in ((sandbox.safe_path, "safe_path"),
                                 (sandbox.read_path, "read_path"),
                                 (sandbox.assert_can_write, "assert_can_write")):
                    try:
                        fn(os.path.join(link, "evil.txt"))
                        raise AssertionError(f"{name} 跟随符号链接越界了")
                    except PermissionError:
                        pass
                mlink = os.path.join(ws, "m_link")
                os.symlink(md, mlink, **kw)
                try:
                    sandbox.safe_path(os.path.join(mlink, "project.md"))
                    raise AssertionError("经符号链接写入 .memory 未被拦截")
                except PermissionError:
                    pass
        finally:
            shutil.rmtree(outside, ignore_errors=True)

        print("✓ 保护区：safe_path / read_path / assert_can_write 三处都拦，普通路径放行，"
              "符号链接不能绕过")
    finally:
        sandbox.set_workspace(old)
        shutil.rmtree(ws, ignore_errors=True)


# ============================================================ 5. 列举与搜索

def test_tools_invisible():
    ws = _new_ws()
    old = sandbox.workspace()
    try:
        sandbox.set_workspace(ws)
        memory.ensure_layout(ws)
        memory.save_project("- 2026-01-01 00:00 · 手动 — 秘密项目规则", ws)
        memory.save_topic("s1", "秘密摘要内容", ws)
        with open(os.path.join(ws, "visible.py"), "w", encoding="utf-8") as f:
            f.write("def hello():\n    return 1\n")
        with open(os.path.join(ws, "visible.txt"), "w", encoding="utf-8") as f:
            f.write("普通文本 secret-token\n")

        import tools  # noqa: F401  —— 加载插件注册表
        from core.registry import REGISTRY

        listing = REGISTRY.call("list_dir", {"path": "."})
        assert "visible.py" in listing
        assert ".memory" not in listing, listing

        globbed = REGISTRY.call("glob_files", {"pattern": "**/*.md"})
        assert "project.md" not in globbed, globbed

        grepped = REGISTRY.call("grep", {"pattern": "秘密"})
        assert "秘密" not in grepped, grepped
        # 对照：普通文件里的内容能搜到（证明 grep 本身工作正常）
        grepped2 = REGISTRY.call("grep", {"pattern": "secret-token"})
        assert "secret-token" in grepped2, grepped2

        counted = REGISTRY.call("count_code_lines", {"path": "."})
        # .memory 下没有代码，但确保统计没被保护区内容干扰（不报错）
        assert "Python" in counted or "统计" in counted

        # 直接按路径读也拒绝（registry.call 把异常转成 [error] 文本）
        denied = REGISTRY.call("read_file", {"path": ".memory/project.md"})
        assert denied.startswith("[error]") and ".memory" in denied, denied
        print("✓ 工具层：list_dir / glob / grep / read_file 都看不见、读不到 .memory")
    finally:
        sandbox.set_workspace(old)
        shutil.rmtree(ws, ignore_errors=True)


# ============================================================ 6. run_python 护栏

def test_python_prelude():
    ws = _new_ws()
    outside = tempfile.mkdtemp(prefix="ma-prelude-out-")
    try:
        md = memory.ensure_layout(ws)
        from core.python_prelude import build_prelude

        script = os.path.join(tempfile.gettempdir(), "_ma_mem_prelude.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(build_prelude(ws))
            f.write(
                "\nimport pathlib, shutil\n"
                "def expect(fn, label):\n"
                "    try:\n"
                "        fn(); print('FAIL', label)\n"
                "    except PermissionError: print('OK', label)\n"
                f"expect(lambda: open(r'{md}\\project.md', 'w'), 'open-w')\n"
                f"expect(lambda: open(r'{md}\\project.md').read(), 'open-r')\n"
                f"expect(lambda: pathlib.Path(r'{md}\\x').write_text('x'), 'pathlib-w')\n"
                f"expect(lambda: shutil.rmtree(r'{md}'), 'rmtree')\n"
                # sqlite3 与链接：Path/bytes 参数、指向区外的链接都不能成为绕过口
                "import os as _os, sqlite3\n"
                f"_OUT = r'{outside}'\n"
                "def expect2(fn, label):\n"
                "    try:\n"
                "        fn(); print('BAD2', label)\n"
                "    except PermissionError: print('PASS2', label)\n"
                f"expect2(lambda: sqlite3.connect(_OUT + r'{os.sep}db.sqlite'), 'sqlite-str')\n"
                "expect2(lambda: sqlite3.connect(pathlib.Path(_OUT) / 'db.sqlite'), 'sqlite-path')\n"
                f"expect2(lambda: _os.symlink(_OUT, _os.path.join(r'{ws}', 'lnk_out')), 'symlink-out')\n"
                f"expect2(lambda: pathlib.Path(r'{ws}', 'lnk_m').symlink_to(r'{md}'), 'symlink-mem')\n"
                "# 保护区外照常\n"
                f"open(r'{ws}\\inside.txt', 'w').write('y')\n"
                "print('OK inside')\n"
            )
        r = subprocess.run([sys.executable, script], capture_output=True, text=True)
        out = r.stdout
        assert r.returncode == 0, r.stderr
        assert "FAIL" not in out, out
        assert "BAD2" not in out, out
        assert out.count("OK") == 5, out
        assert out.count("PASS2") == 4, out
        print("✓ prelude：open 读/写、pathlib、rmtree / sqlite3 / 链接 全拦；区内写正常")
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


# ============================================================ 7. shell 检测

def test_shell_guard():
    ws = _new_ws()
    outside = tempfile.mkdtemp(prefix="ma-shell-out-")
    old = sandbox.workspace()
    try:
        sandbox.set_workspace(ws)
        blocked = [
            "del .memory\\project.md",
            "rmdir /s .memory",
            "echo hacked > .memory/project.md",
            "echo x >> .memory\\topics\\s1.md",
            "copy a.txt .memory\\a.txt",
            "move a.txt .memory\\a.txt",
            "mkdir .memory\\topics",
            # 紧贴重定向 + POSIX 绝对路径（老正则的 lookbehind 会整个漏掉）
            "echo x>/etc/passwd",
            # 带空格的引号路径要能解析出来
            f'echo x > "{os.path.join(outside, "dir with space", "f.txt")}"',
            # 写动词 + 区外绝对路径
            f"rm -rf {outside}",
            # 前置环境变量赋值不能让动词识别失效
            f"FOO=bar rm -rf {outside}",
            # `;` 分隔的语句也要检查
            f"touch a.txt; rm -rf {outside}",
        ]
        for cmd in blocked:
            assert sandbox.check_command_write(cmd) is not None, f"未拦截：{cmd}"
        allowed = [
            "del normal.txt",
            "del /f normal.txt",
            "echo hi > normal.txt",
            "copy a.txt b.txt",
            "mkdir output",
            "dir .",
            "dir /s",
            "echo done",
        ]
        for cmd in allowed:
            assert sandbox.check_command_write(cmd) is None, f"误拦：{cmd}"
        print("✓ shell：重定向 / 删除 / 复制移动 / 建目录到 .memory 全拦，普通写不误伤")
    finally:
        sandbox.set_workspace(old)
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


# ============================================================ 8. 注入渲染

def test_render_block():
    block = memory.render_memory_block(
        user_text="用户偏好简洁",
        project_text="项目用 PowerShell 5",
        topic_texts=["历史摘要甲"],
        budget=800,
    )
    # 顺序：用户偏好 → 项目记忆 → 历史摘要
    iu = block.index("用户偏好")
    ip = block.index("项目规则") if "项目规则" in block else block.index("当前项目记忆")
    it = block.index("历史摘要")
    assert iu < ip < it, block
    assert "用户偏好简洁" in block and "PowerShell 5" in block and "历史摘要甲" in block
    assert "不是当前指令" in block   # 防止记忆内容被当成新指令

    # 空记忆不产生任何段落
    assert memory.render_memory_block(user_text="", project_text="", topic_texts=[]) == ""

    # 预算裁剪：用户画像全保留，topics 被裁并带省略标记
    small = memory.render_memory_block(
        user_text="u" * 80, project_text="", topic_texts=["t" * 500], budget=200
    )
    assert "u" * 80 in small
    assert "省略" in small
    print("✓ 注入：三层顺序、防指令注入提示、空记忆不渲染、超预算裁剪 topics")


# ============================================================ 9. 记忆分级

def test_entry_kinds_and_prune():
    ws = _new_ws()
    try:
        # 新格式：写入时带类型
        assert memory.append_project_note("项目统一用 PowerShell 5", workspace=ws, kind="rule")
        assert memory.append_project_note("决定前端不引入新 UI 库", workspace=ws, kind="decision")
        assert memory.append_project_note("本机换页量 30MB", workspace=ws, kind="fact")
        entries = memory._parse_entries(memory.load_project(ws))
        assert [e["kind"] for e in entries] == ["rule", "decision", "fact"], entries
        assert entries[0]["source"] == "对话"

        # 旧格式（无 [kind]）按 fact 解析，历史数据不丢
        memory.save_project("- 2026-01-01 00:00 · 手动 — 旧条目\n", ws)
        old = memory._parse_entries(memory.load_project(ws))
        assert old[0]["kind"] == "fact" and old[0]["body"] == "旧条目" and old[0]["source"] == "手动"

        # 中文别名归一；非法值返回 None（由调用方决定丢弃）
        assert memory.resolve_kind("偏好") == "pref"
        assert memory.resolve_kind("踩坑") == "pitfall"
        assert memory.resolve_kind("bogus") is None
        assert memory.normalize_kind("bogus") == "fact"

        # 注入排序：规则 → 决定 → 事实（预算不够时先丢事实）
        text = "\n".join([
            "- 2026-01-01 00:00 · [fact] 对话 — 一次性事实",
            "- 2026-01-02 00:00 · [rule] 对话 — 重要规则",
            "- 2026-01-03 00:00 · [decision] 对话 — 已做决定",
        ])
        ordered = memory.order_entries(text)
        assert (
            ordered.index("重要规则") < ordered.index("已做决定") < ordered.index("一次性事实")
        ), ordered

        # 容量淘汰：写满后新进的事实优先被淘汰（噪声不占额度）
        memory.save_project("", ws)
        for i in range(memory.PROJECT_KEEP):
            memory.append_project_note(f"规则{i}", workspace=ws, kind="rule")
        assert memory.append_project_note("最后一条事实", workspace=ws, kind="fact")
        entries = memory._parse_entries(memory.load_project(ws))
        assert len(entries) == memory.PROJECT_KEEP, len(entries)
        assert all(e["kind"] == "rule" for e in entries), "超限时应先淘汰 fact"
        print("✓ 分级：类型写入/解析、旧格式兼容、注入排序、超限先淘汰事实")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_extract_notes_filter():
    from agent import Agent
    raw = [
        {"kind": "pref", "note": "用户喜欢浅色主题"},
        "旧格式字符串条目",
        {"kind": "fact", "note": "一次性任务事实"},
        {"kind": "bogus", "note": "非法类型"},
        {"note": "没写类型"},
        None,
    ]
    # 项目层：fact（一次性事实）与非法类型都被丢弃，缺省类型补 rule
    assert Agent._extract_notes(
        raw, ("pref", "rule", "decision", "pitfall"), "rule"
    ) == [("pref", "用户喜欢浅色主题"), ("rule", "没写类型")]
    # 用户层：字符串与 fact 可保留，缺省类型补 pref
    assert Agent._extract_notes(raw, ("pref", "fact"), "pref") == [
        ("pref", "用户喜欢浅色主题"),
        ("fact", "旧格式字符串条目"),
        ("fact", "一次性任务事实"),
        ("pref", "没写类型"),
    ]
    print("✓ 提炼过滤：项目层丢弃一次性事实/非法类型，缺省类型按层级补齐")


def test_select_and_clip():
    text = "\n".join([
        "- 2026-01-01 00:00 · [rule] 对话 — 项目统一用 PowerShell 5",
        "- 2026-01-01 00:00 · [rule] 对话 — 前端必须保持中文界面",
        "- 2026-01-02 00:00 · [decision] 对话 — 决定不引入新的 UI 库",
        "- 2026-01-03 00:00 · [pitfall] 对话 — 8123 端口被占用时先查 python 进程",
        "- 2026-01-04 00:00 · [pitfall] 对话 — Windows 下 npx 要用 npx.cmd",
        "- 2026-01-05 00:00 · [fact] 对话 — random_100.csv 有 100 列",
    ])
    picked = memory.select_project(text, "8123 端口被占用怎么排查", keep_rest=1)
    # 规则全保留（与问题无关也要遵守）
    assert "PowerShell 5" in picked and "中文界面" in picked, picked
    # 非规则只留最相关的一条
    assert "8123 端口" in picked, picked
    assert "npx.cmd" not in picked and "random_100.csv" not in picked, picked

    # 相关度打分：精确命中的更高
    assert memory.relevance("端口占用", "8123 端口被占用时先查") > memory.relevance(
        "端口占用", "random_100.csv 有 100 列"
    )

    # 预算截断：保留排在前面的条目并标注省略
    clipped = memory.clip_entries(picked, 120)
    assert "更多记忆条目已省略" in clipped, clipped
    assert "PowerShell 5" in clipped
    print("✓ 检索：规则常驻、非规则按相关度取 top-k、预算按条目截断")


def test_remember_command_parse():
    cases = {
        "请帮我记住 我不喜欢红色": "我不喜欢红色",
        "帮我记住：明天交周报": "明天交周报",
        "请记住 用 PowerShell 5": "用 PowerShell 5",
        "记住：项目用 pnpm": "项目用 pnpm",
        "记一下：端口 8123": "端口 8123",
        "记下来 每周五写周报": "每周五写周报",
        "以后都记住 回答用中文": "回答用中文",
        "以后请记住 先给结论": "先给结论",
    }
    for text, want in cases.items():
        assert memory.parse_remember_command(text) == want, text

    # 反例：问句、无冒号、空内容都不算指令
    assert memory.parse_remember_command("你记住我刚才说的吗") is None
    assert memory.parse_remember_command("记住我") is None
    assert memory.parse_remember_command("请帮我记住") is None
    assert memory.parse_remember_command("请帮我记住   ") is None

    # 画像类型粗判
    assert memory.infer_user_kind("我喜欢简短回答") == "pref"
    assert memory.infer_user_kind("明天下午交周报") == "fact"
    print("✓ 显式指令：8 种句式识别、反例不误触发、画像类型粗判")


# ============================================================ 10. remember 工具

def test_remember_tool():
    ws = _new_ws()
    old = sandbox.workspace()
    try:
        sandbox.set_workspace(ws)
        memory.ensure_layout(ws)

        r = memory.remember("project", "这个项目用 npx.cmd")
        assert r.startswith("[ok] 已记入当前项目记忆"), r
        assert "npx.cmd" in memory.load_project(ws)

        r = memory.remember("project", "这个项目用 npx.cmd")
        assert "未重复记录" in r, r

        r = memory.remember("user", "用户喜欢夜间工作")
        assert r.startswith("[ok] 已记入用户画像"), r

        # 参数校验
        assert memory.remember("bogus", "x").startswith("[error]")
        assert memory.remember("user", "  ").startswith("[error]")
        # 项目层拒收一次性事实（kind=fact），避免记忆被任务数据污染
        assert memory.remember("project", "某个临时文件 331 字节", kind="fact").startswith("[error]")

        # 注册幂等 + 工具表可见
        from core.registry import ToolRegistry
        reg = ToolRegistry()
        memory.register(reg)
        memory.register(reg)
        tool = reg.get("remember")
        assert tool is not None and tool.dangerous is False
        assert set(tool.parameters["required"]) == {"scope", "note"}
        print("✓ remember 工具：两层写入、去重、参数校验、注册幂等")
    finally:
        sandbox.set_workspace(old)
        # 清理用户画像（测试写的是真实 web_data 路径）
        try:
            up = memory.user_profile_path()
            if os.path.isfile(up):
                os.remove(up)
        except OSError:
            pass
        shutil.rmtree(ws, ignore_errors=True)


# ============================================================ 10. Agent 注入

def test_agent_injection():
    ws = _new_ws()
    old = sandbox.workspace()
    try:
        sandbox.set_workspace(ws)
        memory.ensure_layout(ws)
        memory.save_project("- 2026-01-01 00:00 · 手动 — 项目规矩 A", ws)
        memory.save_topic("cur", "当前会话摘要", ws)
        memory.save_topic("old", "端口占用的排查结论", ws)

        from agent import Agent
        from core.registry import ToolRegistry

        agent = Agent(
            verbose=False,
            registry=ToolRegistry(),
            session_id="cur",
            client=object(),      # 假客户端：注入阶段不会真的调用
            approval_mode="full",
        )
        agent._sync_system_prompt("随便问点")
        sysp = agent.messages[0]["content"]
        assert "项目规矩 A" in sysp
        assert "当前会话摘要" in sysp
        # 关键词命中应召回旧摘要
        agent._sync_system_prompt("端口占用怎么回事")
        assert "端口占用的排查结论" in agent.messages[0]["content"]

        # 无 session_id 时不注入 topics，但用户/项目层仍在
        agent.session_id = None
        agent._sync_system_prompt("端口占用怎么回事")
        sysp2 = agent.messages[0]["content"]
        assert "项目规矩 A" in sysp2
        assert "端口占用的排查结论" not in sysp2
        print("✓ Agent 注入：项目记忆常驻、当前摘要必带、关键词召回、无 session 时不带 topics")
    finally:
        sandbox.set_workspace(old)
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    print("项目根:", HERE)
    print("---")
    test_layout_and_hidden()
    test_gitignore()
    test_append_and_dedup()
    test_topics_and_prune()
    test_sandbox_guard()
    test_tools_invisible()
    test_python_prelude()
    test_shell_guard()
    test_render_block()
    test_entry_kinds_and_prune()
    test_extract_notes_filter()
    test_select_and_clip()
    test_remember_command_parse()
    test_remember_tool()
    test_agent_injection()
    print("---")
    print("全部通过 ✅")
