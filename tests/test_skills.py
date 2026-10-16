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


if __name__ == "__main__":
    print("项目根:", HERE)
    print("---")
    test_parse_frontmatter()
    test_loader_discover_and_load()
    test_loader_conflict_isolation()
    test_reload()
    test_use_skill_tool()
    test_use_skill_not_truncated()
    print("---")
    print("全部通过 ✅")