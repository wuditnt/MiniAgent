"""
本地技能（Skill）加载器：发现 → 解析 → 注入清单 → 按需加载正文。

一个 skill 是什么？
    一个目录 + 一张 SKILL.md。SKILL.md 顶部是可选的 YAML frontmatter
    （用 `name` / `description` 声明），正文是给模型的完整操作指引。

    与「工具」不同：工具是模型能*调用*的能力，skill 是模型能*遵循*的工作流。
    两者互补 —— 一个 skill 往往编排多个工具完成一件有章法的事。

怎么生效（清单 + 按需加载，不解耦上下文就永远塞不下几十个技能）：
    1. 启动时把所有 skill 的「名字 + 一句话描述」变成一张清单，注入 system prompt；
    2. 模型判断任务命中某个 skill 时，调用内置的 use_skill 工具，
       把该 skill 的正文拉进上下文，再照着做。

设计原则（与 ToolLoader 一脉相承）：
    1. 约定优于配置 —— skills/、skills_user/ 下的子目录只要含 SKILL.md 就被发现。
    2. 故障隔离 —— 某个 skill 解析失败只记录在报告里，不影响其它 skill。
    3. 可热重载 —— /skills reload 重新扫描，不用重启。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from core import envs
from core._paths import data_dir
from core.registry import Tool, ToolRegistry
from core.schema import schema_from_callable

DEFAULT_SKILL_DIRS = ("skills", "skills_user")


# ------------------------------------------------------------------ 启用状态
#
# 停用是「用户偏好」而不是技能内容，所以不写进 SKILL.md，单独落一份 JSON：
#   {"disabled": ["builtin:csv-analysis", ...]}
# 默认全部启用，只记停用的 —— 新加的技能天然是启用状态，不需要迁移。

def default_state_path() -> str:
    """启用状态文件：<数据目录>/skills_state.json（不随技能目录走）。"""
    return os.path.join(data_dir(), "skills_state.json")


def load_disabled(path: str) -> set[str]:
    """读停用集合；文件不存在或损坏时当作「全部启用」。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    items = data.get("disabled") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items if str(x).strip()}


def save_disabled(path: str, disabled: set[str]) -> None:
    """原子写：先写临时文件再 rename，避免半截 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"disabled": sorted(disabled)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def default_dirs() -> list[str]:
    """技能目录。可用环境变量 AGENT_SKILL_DIRS 覆盖（逗号或分号分隔，替换默认）。

    设 AGENT_NO_SKILLS=1 可完全关闭技能加载。
    """
    if envs.get_bool("MINI_AGENT_NO_SKILLS", False, alias="AGENT_NO_SKILLS"):
        return []
    raw = envs.get_str("MINI_AGENT_SKILL_DIRS", alias="AGENT_SKILL_DIRS")
    if not raw:
        return list(DEFAULT_SKILL_DIRS)
    return [d.strip() for d in raw.replace(";", ",").split(",") if d.strip()]


# ------------------------------------------------------------------ frontmatter

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md。返回 (meta, body)。

    meta 只支持最简单的 `key: value` 行（避免引入 yaml 依赖），
    由 `---` 包裹时可写可不写；认不出就整篇当正文。
    """
    text = text.lstrip("\ufeff")
    meta: dict[str, str] = {}
    body = text.strip()

    if text.startswith("---"):
        lines = text.splitlines()
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            for line in lines[1:end]:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            body = "\n".join(lines[end + 1:]).strip()

    return meta, body


# ------------------------------------------------------------------ 数据结构

@dataclass
class Skill:
    name: str           # 模型与 use_skill 都用它定位（frontmatter 的 name 或目录名）
    description: str    # 一句话说明，进清单，模型靠它判断何时用
    dir: str            # 目录绝对路径
    path: str           # SKILL.md 绝对路径
    body: str           # frontmatter 之后的正文（use_skill 返回它）
    # 来源分类：builtin = 项目内置 skills/；custom = 用户自己创建（skills_user/ 等）。
    # 未来「由对话自动生成」的技能也归入 custom —— 它们同样属于用户创建的内容。
    source: str = "builtin"
    #: 稳定标识：`<source>:<目录名>`，如 custom:my-skill。用于启停状态与增删改接口，
    #: 不随 frontmatter 里的中文名变化，也不含路径分隔符（无需担心目录穿越）。
    id: str = ""
    enabled: bool = True
    #: 只有自定义技能可编辑 / 删除（系统内置随代码发布，改了就丢）
    editable: bool = False


@dataclass
class SkillLoadReport:
    loaded: list[str] = field(default_factory=list)          # 成功加载的 skill 名
    failed: dict[str, str] = field(default_factory=dict)     # 名 -> 原因（解析失败 / 冲突）

    @property
    def count(self) -> int:
        return len(self.loaded)

    def summary(self) -> str:
        bits = [f"技能 {self.count} 个"]
        if self.failed:
            bits.append(f"失败 {len(self.failed)}")
        return " | ".join(bits)


# ------------------------------------------------------------------ 加载器

class SkillLoader:
    """管理若干个技能目录，产出可用技能清单与正文。"""

    def __init__(
        self,
        project_root: str,
        dirs: list[str] | None = None,
        state_path: str | None = None,
    ) -> None:
        self.root = os.path.abspath(project_root)
        self.dirs = list(dirs) if dirs is not None else default_dirs()
        self._skills: dict[str, Skill] = {}
        # 启用状态：默认落在数据目录；测试可以传临时文件，避免污染真实配置
        self.state_path = state_path or default_state_path()
        self._disabled = load_disabled(self.state_path)

    # ------------------------------------------------------------ 发现

    def discover(self) -> list[tuple[str, str, str]]:
        """扫描技能目录，返回 [(目录名, skill名, SKILL.md绝对路径)]。"""
        found: list[tuple[str, str, str]] = []
        for d in self.dirs:
            base = os.path.join(self.root, d)
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.listdir(base)):
                if entry.startswith(".") or entry.startswith("_"):
                    continue
                full = os.path.join(base, entry)
                if not os.path.isdir(full):
                    continue
                skill_md = os.path.join(full, "SKILL.md")
                if os.path.isfile(skill_md):
                    found.append((d, entry, skill_md))
        return found

    # ------------------------------------------------------------ 加载

    def load_all(self) -> SkillLoadReport:
        report = SkillLoadReport()
        self._skills = {}
        for base, name, path in self.discover():
            self._load_one(base, name, path, report)
        return report

    def reload(self) -> SkillLoadReport:
        """重新扫描（覆盖式，最简单也最可预测 —— 删掉的 skill 自然消失）。"""
        return self.load_all()

    def _load_one(
        self, base_dir: str, dir_name: str, path: str, report: SkillLoadReport
    ) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception as e:  # noqa: BLE001 —— 坏文件不能带走主程序
            report.failed[dir_name] = f"{type(e).__name__}: {e}"
            return

        meta, body = parse_frontmatter(text)
        name = (meta.get("name") or dir_name).strip()
        description = (meta.get("description") or "").strip()
        if not description:
            # 取正文首行兜底，避免清单里出现空描述
            for line in body.splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    description = line.strip().lstrip("#").strip()
                    break

        if name in self._skills:
            report.failed[dir_name] = f"技能名冲突：{name}（已被 {self._skills[name].dir} 占用）"
            return

        # 默认目录 skills/ 视为内置，其余（skills_user 等）视为用户自定义
        source = "builtin" if base_dir == DEFAULT_SKILL_DIRS[0] else "custom"
        skill_id = f"{source}:{dir_name}"

        self._skills[name] = Skill(
            name=name,
            description=description or name,
            dir=os.path.dirname(path),
            path=path,
            body=body,
            source=source,
            id=skill_id,
            enabled=skill_id not in self._disabled,
            editable=source == "custom",
        )
        report.loaded.append(name)

    # ------------------------------------------------------------ 查询

    def all(self) -> list[Skill]:
        return [self._skills[n] for n in sorted(self._skills)]

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def by_id(self, skill_id: str) -> Skill | None:
        """按稳定 id 查找（管理接口用；找不到返回 None）。"""
        for s in self._skills.values():
            if s.id == skill_id:
                return s
        return None

    def names(self) -> list[str]:
        return sorted(self._skills)

    def enabled(self) -> list[Skill]:
        """启用的技能（注入清单、输入框选择器都只看这一份）。"""
        return [s for s in self.all() if s.enabled]

    # ------------------------------------------------------------ 启用状态

    def set_enabled(self, skill_id: str, enabled: bool) -> Skill | None:
        """切换启用状态并落盘；返回更新后的技能。"""
        s = self.by_id(skill_id)
        if s is None:
            return None
        if enabled:
            self._disabled.discard(skill_id)
        else:
            self._disabled.add(skill_id)
        save_disabled(self.state_path, self._disabled)
        s.enabled = enabled
        return s

    def forget(self, skill_id: str) -> None:
        """技能被删除后清掉它的停用记录，避免配置里留垃圾。"""
        if skill_id in self._disabled:
            self._disabled.discard(skill_id)
            save_disabled(self.state_path, self._disabled)

    def render_index(self) -> str:
        """生成注入 system prompt 的「可用技能清单」。没有启用的技能时返回空串。"""
        skills = self.enabled()
        if not skills:
            return ""
        lines = [
            "## 可用技能（Skills）",
            "",
            "下面是本项目预设的技能，每一行对应一个可复用的操作指引。",
            "当任务与某个技能的描述匹配时，先调用 use_skill 工具加载它的完整指引，再照着执行。",
            "",
        ]
        for s in skills:
            lines.append(f"- {s.name}：{s.description}")
        return "\n".join(lines)


# ------------------------------------------------------------------ 内置工具

def register_use_skill_tool(registry: ToolRegistry, loader: SkillLoader) -> Tool:
    """把 use_skill 注册成一个「技能通道」的内置工具。

    它不属于任何工具模块，直接进注册表，group=技能。模型在 system prompt 里
    看到技能清单后，按需调用它把某个 skill 的正文拉进上下文。
    """

    def use_skill(name: str) -> str:
        """加载并返回指定技能的完整操作指引。

        Args:
            name: 技能名称（见 system prompt 里的「可用技能」清单）
        """
        s = loader.get(name)
        if s is None:
            available = ", ".join(loader.names()) or "（当前没有任何技能）"
            return f"[error] 未知技能：{name}。可用技能：{available}"
        if not s.enabled:
            return f"[error] 技能「{name}」已停用，请在技能管理中启用后再使用。"
        return s.body

    use_skill.__name__ = "use_skill"
    t = Tool(
        name="use_skill",
        description="加载指定技能的完整操作指引。当任务匹配 system prompt 里「可用技能」清单中的某项时，先调用它获取详细步骤，再照着执行。",
        parameters=schema_from_callable(use_skill),
        func=use_skill,
        group="技能",
        source="core",
        module="skills",
        no_clip=True,   # 技能正文是给模型的完整指引，截断会丢步骤 —— 不走默认的 clip
    )
    try:
        registry.add(t)
    except ValueError as e:
        # 名字冲突（理论上不会）不该阻断启动
        registry.skipped[t.name] = str(e)
    return t


def load_skills(project_root: str, dirs: list[str] | None = None) -> SkillLoader:
    """便捷入口：建加载器并加载全部技能，返回可继续查询/热重载的加载器。"""
    loader = SkillLoader(project_root, dirs)
    loader.load_all()
    return loader