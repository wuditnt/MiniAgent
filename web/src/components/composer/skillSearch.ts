/**
 * 技能模糊搜索（纯函数，不依赖 React）。
 *
 * 匹配口径：
 *  · 归一化 = 转小写 + 去掉所有空白，所以 "csv" 能命中 "CSV 分析"；
 *  · 名称前缀 > 名称/目录名包含 > 描述包含；
 *  · 同分按名称长度升序、再按字典序，结果稳定可预测。
 *
 * 不引入拼音 / 首字母库：中文名直接子串匹配已覆盖主要场景，
 * 需要时在这里加一层转换即可，调用方不用改。
 */
import type { SkillItem } from "../../types";

function normalize(text: string): string {
  return text.toLowerCase().replace(/\s+/g, "");
}

/** 目录名部分（id 形如 `custom:my-skill`），让英文目录名也能被搜到 */
function slugOf(skill: SkillItem): string {
  const i = skill.id.indexOf(":");
  return i >= 0 ? skill.id.slice(i + 1) : skill.id;
}

export function matchSkills(skills: SkillItem[], query: string): SkillItem[] {
  const q = normalize(query);
  if (!q) return skills;

  const scored: { skill: SkillItem; rank: number }[] = [];
  for (const skill of skills) {
    const name = normalize(skill.name);
    const slug = normalize(slugOf(skill));
    let rank = -1;
    if (name.startsWith(q)) rank = 0;
    else if (name.includes(q) || slug.includes(q)) rank = 1;
    else if (normalize(skill.description).includes(q)) rank = 2;
    if (rank >= 0) scored.push({ skill, rank });
  }

  scored.sort(
    (a, b) =>
      a.rank - b.rank ||
      a.skill.name.length - b.skill.name.length ||
      a.skill.name.localeCompare(b.skill.name, "zh")
  );
  return scored.map((x) => x.skill);
}
