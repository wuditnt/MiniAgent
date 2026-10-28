/**
 * 输入框工具栏的技能选择器（单选）。
 *
 * 两类来源：内置（skills/）与自定义（skills_user/ 等）；`auto` 是给「未来由对话
 * 自动创建技能」预留的分类，现在不会有数据，但分组逻辑已经就位。
 */
import { DownOutlined, ReadOutlined } from "@ant-design/icons";
import { Dropdown } from "antd";
import type { MenuProps } from "antd";
import type { SkillItem } from "../../types";

const SOURCE_LABEL: Record<SkillItem["source"], string> = {
  builtin: "系统内置",
  custom: "自定义",
  auto: "自动创建",
};

function groupLabel(source: SkillItem["source"], count: number) {
  return `${SOURCE_LABEL[source]}${count ? `（${count}）` : ""}`;
}

export default function SkillPicker({
  skills,
  value,
  open,
  onOpenChange,
  onPick,
}: {
  skills: SkillItem[] | null;
  value: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (name: string) => void;
}) {
  const list = skills ?? [];
  const items: MenuProps["items"] = skills === null
    ? [{ key: "__loading", label: "加载中…", disabled: true }]
    : (["builtin", "custom", "auto"] as SkillItem["source"][])
        .map((source) => {
          const group = list.filter((s) => s.source === source);
          // 空分组只在「自定义」下提示一次，内置/自动为空时不占位
          if (!group.length && source !== "custom") return null;
          return {
            type: "group" as const,
            label: groupLabel(source, group.length),
            children: group.length
              ? group.map((s) => ({
                  key: s.name,
                  label: (
                    <span className="skill-menu-item">
                      <span className="skill-menu-name">{s.name}</span>
                      <span className="skill-menu-desc">{s.description}</span>
                    </span>
                  ),
                }))
              : [{ key: `__empty-${source}`, label: "暂无自定义技能", disabled: true }],
          };
        })
        .filter(Boolean) as MenuProps["items"];

  return (
    <Dropdown
      trigger={["click"]}
      placement="topLeft"
      open={open}
      onOpenChange={onOpenChange}
      menu={{
        className: "skill-menu",
        selectable: true,
        selectedKeys: value ? [value] : [],
        items,
        onClick: ({ key }) => {
          if (key.startsWith("__")) return;
          onPick(key);
        },
      }}
    >
      <span className="tb-mode tb-skill" title={value ? `技能：${value}` : "选择技能"}>
        <ReadOutlined />
        <span className="tb-mode-text">{value || "技能"}</span>
        <DownOutlined className="tb-mode-caret" />
      </span>
    </Dropdown>
  );
}
