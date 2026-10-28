"""
技能层测试。不需要 API Key，不花任何费用。

覆盖三处容易出错的地方：
  1. frontmatter 解析 —— 有 / 无 / 残缺三种情况
  2. SkillLoader     —— 发现、名字回退、清单渲染、重名冲突隔离、热重载
  3. use_skill 工具   —— 注册、schema 推导、按名取正文、未知技能报错
"""

from __future__ import annotations

import os
import shutil
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

from core.registry import ToolRegistry  # noqa: E402
from skills import SkillLoader, parse_frontmatter, register_use_skill_tool  # noqa: E402


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ============================================================ 1. frontmatter

def test_parse_frontmatter():
    meta, body = parse_frontmatter(
        "---\nname: foo\ndescription: 做某事\n---\n\n# 步骤\n第一步"
    )
    assert meta == {"name": "foo", "description": "做某事"}, meta
    assert body == "# 步骤\n第一步", body

    meta, body = parse_frontmatter("直接正文，没有 frontmatter")
    assert meta == {}, meta
    assert body == "直接正文，没有 frontmatter", body

    meta, _ = parse_frontmatter("---\n只有开头没有结尾")
    assert meta == {}, meta
    print("✓ frontmatter：有 / 无 / 残缺三种情况解析正确")


# ============================================================ 2. 加载器

def test_loader_discover_and_load():
    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "alpha", "SKILL.md"),
               "---\nname: alpha\ndescription: 处理 A 类任务\n---\n\n# Alpha\n步骤甲")
        _write(os.path.join(root, "skills", "beta", "SKILL.md"),
               "---\ndescription: 处理 B 类任务（没写 name，用目录名）\n---\n\n# Beta\n步骤乙")

        loader = SkillLoader(root, dirs=["skills"])
        report = loader.load_all()
        assert report.count == 2, report
        assert set(report.loaded) == {"alpha", "beta"}, report.loaded

        a = loader.get("alpha")
        assert a.description == "处理 A 类任务"
        assert a.body.startswith("# Alpha")
        b = loader.get("beta")
        assert b.name == "beta", "没写 name 时应回退到目录名"
        assert "处理 B 类任务" in b.description

        index = loader.render_index()
        assert "alpha" in index and "beta" in index
        assert "处理 A 类任务" in index
        print("✓ 发现/加载：目录名回退、description 抽取、清单渲染")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_loader_conflict_isolation():
    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "dup1", "SKILL.md"),
               "---\nname: same\n---\n\n正文1")
        _write(os.path.join(root, "skills", "dup2", "SKILL.md"),
               "---\nname: same\n---\n\n正文2")
        loader = SkillLoader(root, dirs=["skills"])
        report = loader.load_all()
        assert report.count == 1, report
        assert len(report.failed) == 1, report.failed
        assert "冲突" in list(report.failed.values())[0]
        print("✓ 故障隔离：重名冲突只记录、不覆盖、不中断")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reload():
    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "one", "SKILL.md"),
               "---\nname: one\n---\n\n1")
        loader = SkillLoader(root, dirs=["skills"])
        loader.load_all()
        assert "one" in loader.names()

        shutil.rmtree(os.path.join(root, "skills", "one"))
        _write(os.path.join(root, "skills", "two", "SKILL.md"),
               "---\nname: two\n---\n\n2")
        report = loader.reload()
        assert report.count == 1
        assert loader.names() == ["two"], loader.names()
        print("✓ 热重载：覆盖式重扫，删除的消失、新增的出现")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ============================================================ 3. use_skill

def test_use_skill_tool():
    root = tempfile.mkdtemp()
    reg = ToolRegistry()
    try:
        _write(os.path.join(root, "skills", "demo", "SKILL.md"),
               "---\nname: demo\ndescription: 演示技能\n---\n\n# 演示正文\n步骤")
        loader = SkillLoader(root, dirs=["skills"])
        loader.load_all()
        register_use_skill_tool(reg, loader)

        t = reg.get("use_skill")
        assert t is not None
        assert t.group == "技能"
        s = t.parameters
        assert s["properties"]["name"]["type"] == "string"
        assert "name" in s["required"]

        out = reg.call("use_skill", {"name": "demo"})
        assert "# 演示正文" in out, out

        err = reg.call("use_skill", {"name": "不存在"})
        assert "[error]" in err and "demo" in err, err
        print("✓ use_skill：注册、schema 推导、按名取正文、未知技能报错")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_use_skill_not_truncated():
    root = tempfile.mkdtemp()
    reg = ToolRegistry()
    try:
        from core.registry import tool as _tool
        from core.sandbox import MAX_OUTPUT_CHARS

        long_body = "# 超长技能\n" + "这是某条细节步骤，拼起来要超过默认截断上限。\n" * 800
        assert len(long_body) > MAX_OUTPUT_CHARS
        _write(os.path.join(root, "skills", "long", "SKILL.md"),
               "---\nname: long\n---\n\n" + long_body)
        loader = SkillLoader(root, dirs=["skills"])
        loader.load_all()
        register_use_skill_tool(reg, loader)

        out = reg.call("use_skill", {"name": "long"})
        stored = loader.get("long").body
        assert out == stored, "use_skill 声明了 no_clip，输出必须与存储的完整正文一致"
        assert len(out) > MAX_OUTPUT_CHARS, "测试数据应超过截断上限，否则测不到东西"

        # 对照：普通工具的超长返回仍走默认截断策略
        @_tool(registry=reg, group="测试")
        def shout(text: str) -> str:
            """重复文本。"""
            return text * (MAX_OUTPUT_CHARS * 3)   # 远超上限，必被截

        out2 = reg.call("shout", {"text": "x"})
        assert "已截断" in out2, "普通工具输出仍应被截断（带截断标记）"
        print("✓ no_clip：use_skill 全文返回不被截断，普通工具仍走截断")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_skill_source():
    """来源分类：内置 / 用户自定义（两类）。"""
    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "builtin-one", "SKILL.md"),
               "---\nname: builtin-one\n---\n\n内置正文")
        _write(os.path.join(root, "skills_user", "my-one", "SKILL.md"),
               "---\nname: my-one\n---\n\n自定义正文")
        _write(os.path.join(root, "skills_user", "origin-auto", "SKILL.md"),
               "---\nname: origin-auto\norigin: auto\n---\n\n自动生成正文")
        loader = SkillLoader(root, dirs=["skills", "skills_user"])
        loader.load_all()
        assert loader.get("builtin-one").source == "builtin"
        assert loader.get("my-one").source == "custom"
        # 不再有 auto 分类：未来自动生成的技能也属于用户自定义
        assert loader.get("origin-auto").source == "custom"
        print("✓ 来源分类：skills/ → builtin，其余（含 origin:auto）→ custom")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_enabled_state_and_editability():
    """启用状态：默认启用、停用后不进清单与注入、落盘后重新加载仍生效。"""
    from webui import _skill_context, _skills_from_message

    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "alpha", "SKILL.md"),
               "---\nname: alpha\ndescription: 内置技能\n---\n\n内置正文")
        _write(os.path.join(root, "skills_user", "my-one", "SKILL.md"),
               "---\nname: my-one\ndescription: 自定义技能\n---\n\n自定义正文")
        state = os.path.join(root, "skills_state.json")
        loader = SkillLoader(root, dirs=["skills", "skills_user"], state_path=state)
        loader.load_all()

        builtin, custom = loader.get("alpha"), loader.get("my-one")
        assert builtin.id == "builtin:alpha" and not builtin.editable
        assert custom.id == "custom:my-one" and custom.editable
        assert builtin.enabled and custom.enabled

        loader.set_enabled(builtin.id, False)
        assert not loader.get("alpha").enabled
        assert "alpha" not in loader.render_index()
        assert "my-one" in loader.render_index()
        assert _skill_context(loader, ["alpha"])[1] == []
        assert _skills_from_message(loader, "\x01alpha\x01 你好")[0] == []
        assert os.path.isfile(state), "停用状态应落盘"

        # 重新加载（模拟重启）：停用状态还在
        again = SkillLoader(root, dirs=["skills", "skills_user"], state_path=state)
        again.load_all()
        assert not again.get("alpha").enabled
        assert again.get("alpha").body, "停用不影响正文可读"

        again.set_enabled(builtin.id, True)
        assert again.get("alpha").enabled
        assert "alpha" in again.render_index()
        print("✓ 启用状态：默认启用、停用后不进清单/注入、重启后保持")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_skills_from_message():
    """从消息里的不可见包裹字符中解析技能：顺序、去重、3 次上限、未知忽略。"""
    from webui import _skills_from_message

    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "alpha", "SKILL.md"),
               "---\nname: alpha\n---\n\nA")
        _write(os.path.join(root, "skills", "beta", "SKILL.md"),
               "---\nname: beta\n---\n\nB")
        loader = SkillLoader(root, dirs=["skills"])
        loader.load_all()

        # 正常：按出现顺序、重复去重、未知名字被忽略但保留正文
        msg = "\x01alpha\x01 \x01beta\x01 \x01alpha\x01 分析这个文件"
        names, cleaned = _skills_from_message(loader, msg)
        assert names == ["alpha", "beta"], names
        assert cleaned == "alpha beta alpha 分析这个文件", cleaned

        # 超过 3 次出现：只认前 3 个（重复也计数），注不存在的多次占位
        msg = "\x01alpha\x01\x01beta\x01\x01nope\x01\x01beta\x01"
        names, _ = _skills_from_message(loader, msg)
        assert names == ["alpha", "beta"], names

        # 快速对话：loader=None 时只清理包裹符，不识别技能
        names, cleaned = _skills_from_message(None, "\x01alpha\x01 你好")
        assert names == [] and cleaned == "alpha 你好", (names, cleaned)
        print("✓ 消息解析技能：顺序、去重、3 次上限、未知忽略、清理包裹符")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_skill_context_injection():
    """Web 端「本轮选中技能」→ system prompt 附加段：多选、去重、保序、预算截断。"""
    from webui import _SKILL_CONTEXT_MAX_CHARS, _skill_context

    root = tempfile.mkdtemp()
    try:
        _write(os.path.join(root, "skills", "demo", "SKILL.md"),
               "---\nname: demo\n---\n\n# 演示正文\n第一步")
        _write(os.path.join(root, "skills", "second", "SKILL.md"),
               "---\nname: second\n---\n\n# 第二个技能\n第二步")
        loader = SkillLoader(root, dirs=["skills"])
        loader.load_all()

        # 单选：不出现优先级说明
        ctx1, used1 = _skill_context(loader, ["demo"])
        assert used1 == ["demo"] and "## 技能：demo" in ctx1, (ctx1, used1)
        assert "优先级更高" not in ctx1

        # 多选：去重、保持选择顺序、忽略未知技能
        ctx, used = _skill_context(loader, ["demo", "second", "demo", "不存在"])
        assert used == ["demo", "second"], used
        assert ctx.index("## 技能：demo") < ctx.index("## 技能：second"), ctx
        assert "前面的优先级更高" in ctx, ctx

        # 未知技能 / 未启用技能加载
        assert _skill_context(loader, ["不存在"]) == ("", [])
        assert _skill_context(None, ["demo"]) == ("", [])

        # 单个技能超过总预算：截断并明确告知模型
        _write(os.path.join(root, "skills", "huge", "SKILL.md"),
               "---\nname: huge\n---\n\n" + "很长的步骤说明。\n" * 4000)
        loader.reload()
        ctx2, used2 = _skill_context(loader, ["huge"])
        assert used2 == ["huge"], used2
        assert "已截断" in ctx2, ctx2[-80:]
        assert len(ctx2) < _SKILL_CONTEXT_MAX_CHARS + 2000, len(ctx2)
        print("✓ 技能注入：多选去重保序、优先级说明、预算截断、未知技能忽略")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    print("项目根:", HERE)
    print("---")
    test_parse_frontmatter()
    test_loader_discover_and_load()
    test_loader_conflict_isolation()
    test_reload()
    test_use_skill_tool()
    test_use_skill_not_truncated()
    test_skill_source()
    test_skills_from_message()
    test_skill_context_injection()
    print("---")
    print("全部通过 ✅")