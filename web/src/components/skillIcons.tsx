/**
 * 技能图标：64×64 圆角方块 + 主题色。
 *
 *  · 系统内置：按「名称 / 目录 / 描述」里的关键词匹配主题（文档、会议、调研、
 *    日志、翻译、演示、表格、图表），新增内置技能不改代码也能落到合适主题；
 *  · 自定义：与输入框里的技能块保持同一枚书籍图标（浅蓝），统一视觉。
 */
import type { ReactNode } from "react";
import {
  BarChartOutlined,
  BookOutlined,
  BugOutlined,
  FileTextOutlined,
  FundProjectionScreenOutlined,
  SearchOutlined,
  TableOutlined,
  TeamOutlined,
  TranslationOutlined,
} from "@ant-design/icons";
import type { SkillItem } from "../types";

interface Theme {
  icon: ReactNode;
  color: string;
  bg: string;
}

const THEMES: Record<string, Theme> = {
  data: { icon: <TableOutlined />, color: "#2f54eb", bg: "rgba(47,84,235,.10)" },
  render: { icon: <BarChartOutlined />, color: "#722ed1", bg: "rgba(114,46,209,.10)" },
  doc: { icon: <FileTextOutlined />, color: "#1677ff", bg: "rgba(22,119,255,.10)" },
  meeting: { icon: <TeamOutlined />, color: "#13c2c2", bg: "rgba(19,194,194,.12)" },
  research: { icon: <SearchOutlined />, color: "#fa8c16", bg: "rgba(250,140,22,.12)" },
  log: { icon: <BugOutlined />, color: "#f5222d", bg: "rgba(245,34,45,.10)" },
  translate: { icon: <TranslationOutlined />, color: "#52c41a", bg: "rgba(82,196,26,.12)" },
  present: { icon: <FundProjectionScreenOutlined />, color: "#eb2f96", bg: "rgba(235,47,150,.10)" },
  generic: { icon: <BookOutlined />, color: "#1677ff", bg: "rgba(22,119,255,.10)" },
};

/** 自定义技能：与输入框技能块一致的书籍图标 */
const CUSTOM: Theme = {
  icon: <BookOutlined />,
  color: "#69b1ff",
  bg: "rgba(105,177,255,.14)",
};

/** 关键词 → 主题；顺序有意义（先匹配到的生效） */
const RULES: [RegExp, string][] = [
  [/csv|表格|数据|统计|excel|data/i, "data"],
  [/render|渲染|可视化|图表|ui\b/i, "render"],
  [/meeting|会议|纪要/i, "meeting"],
  [/research|调研|竞品|检索|简报/i, "research"],
  [/log|日志|排错|故障|triage|debug/i, "log"],
  [/translat|翻译|润色|polish/i, "translate"],
  [/ppt|演示|大纲|presentation|slide/i, "present"],
  [/doc|文档|写作|方案|手册|writer/i, "doc"],
];

function resolveTheme(skill: SkillItem): Theme {
  if (skill.source === "custom") return CUSTOM;
  const haystack = `${skill.id} ${skill.name} ${skill.description}`;
  for (const [re, key] of RULES) {
    if (re.test(haystack)) return THEMES[key];
  }
  return THEMES.generic;
}

export default function SkillIcon({
  skill,
  size = 64,
}: {
  skill: SkillItem;
  size?: number;
}) {
  const theme = resolveTheme(skill);
  return (
    <span
      className="skill-icon-box"
      style={{
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.22),
        background: theme.bg,
        color: theme.color,
        fontSize: Math.round(size * 0.47),
      }}
    >
      {theme.icon}
    </span>
  );
}
