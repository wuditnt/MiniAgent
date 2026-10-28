/**
 * 输入框工具栏的技能选择器（单选）。
 *
 * 面板不用 antd Dropdown：它需要「贴着输入框顶部、宽度与输入框完全一致」。
 * 这里用绝对定位挂在 .composer-slot 上（该容器已是 position: relative），
 * left/right 同时为 0 → 宽度天然跟随输入框，浏览器缩放时自动适应。
 *
 * 两类来源：内置（skills/）与自定义（skills_user/ 等）；`auto` 是给「未来由对话
 * 自动创建技能」预留的分类，现在不会有数据，但分组逻辑已经就位。
 */
import { useEffect, useRef } from "react";
import { DownOutlined, ReadOutlined } from "@ant-design/icons";
import type { SkillItem } from "../../types";

const SOURCE_ORDER: SkillItem["source"][] = ["builtin", "custom", "auto"];

const SOURCE_LABEL: Record<SkillItem["source"], string> = {
  builtin: "系统内置",
  custom: "自定义",
  auto: "自动创建",
};

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
  const rootRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // 点面板/按钮之外的区域或按 Esc 收起
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || rootRef.current?.contains(target)) return;
      onOpenChange(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onOpenChange]);

  const groups = SOURCE_ORDER.map((source) => ({
    source,
    label: SOURCE_LABEL[source],
    items: (skills ?? []).filter((s) => s.source === source),
  }));

  return (
    <span className="skill-picker" ref={rootRef}>
      <span
        className={`tb-mode tb-skill${open ? " active" : ""}`}
        title={value ? `技能：${value}` : "选择技能"}
        onClick={() => onOpenChange(!open)}
      >
        <ReadOutlined />
        <span className="tb-mode-text">{value || "技能"}</span>
        <DownOutlined className="tb-mode-caret" />
      </span>

      {open && (
        <div className="skill-pop" ref={panelRef} role="listbox">
          {skills === null ? (
            <div className="skill-pop-hint">加载中…</div>
          ) : (
            groups.map((group) => {
              // 内置为空时不占位；自定义为空给一句提示，避免面板看起来像坏了
              if (!group.items.length && group.source !== "custom") return null;
              return (
                <div className="skill-pop-group" key={group.source}>
                  <div className="skill-pop-label">
                    {group.label}
                    {group.items.length > 0 && `（${group.items.length}）`}
                  </div>
                  {group.items.length ? (
                    group.items.map((s) => (
                      <div
                        key={s.name}
                        className={`skill-pop-item${value === s.name ? " active" : ""}`}
                        role="option"
                        aria-selected={value === s.name}
                        onClick={() => onPick(s.name)}
                      >
                        <span className="skill-pop-name">{s.name}</span>
                        <span className="skill-pop-desc" title={s.description}>
                          {s.description}
                        </span>
                      </div>
                    ))
                  ) : (
                    <div className="skill-pop-hint">暂无自定义技能</div>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}
    </span>
  );
}
