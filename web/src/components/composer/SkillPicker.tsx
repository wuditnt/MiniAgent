/**
 * 输入框工具栏的技能选择器（树形列表，单选动作）。
 *
 * 面板不用 antd Dropdown：它需要「贴着输入框顶部、宽度与输入框完全一致」。
 * 这里用绝对定位挂在 .composer-slot 上（该容器已是 position: relative），
 * left/right 同时为 0 → 宽度天然跟随输入框，浏览器缩放时自动适应。
 *
 * 选择器不是多选框：分组标题下缩进列出技能，点一下 / 回车就把技能插入输入框
 * 并立即关闭面板；输入框里可以出现多个技能块（由 Chat 控制上限）。
 *
 * 两类来源：系统内置（skills/）与自定义（skills_user/ 等，未来自动生成的技能
 * 也归入这一类）。两类图标形式相同，只靠颜色区分。
 *
 * 键盘：打开时默认高亮第一项，↑/↓ 移动，回车选择，Esc 关闭。按键在 document
 * 捕获阶段拦截 —— 焦点始终在输入框里，必须抢在 Sender 的「回车发送」之前处理。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { BookOutlined, DownOutlined, ReadOutlined } from "@ant-design/icons";
import type { SkillItem } from "../../types";

const SOURCE_ORDER: SkillItem["source"][] = ["builtin", "custom"];

const SOURCE_LABEL: Record<SkillItem["source"], string> = {
  builtin: "系统内置",
  custom: "自定义",
};

/** 两个来源的图标颜色（形式相同，只换颜色） */
const SOURCE_COLOR: Record<SkillItem["source"], string> = {
  builtin: "#1677ff",
  custom: "#69b1ff",
};

export default function SkillPicker({
  skills,
  open,
  onOpenChange,
  onSelect,
}: {
  skills: SkillItem[] | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: (item: SkillItem) => void;
}) {
  const rootRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // 键盘高亮项
  const [active, setActive] = useState(0);

  const groups = useMemo(
    () =>
      SOURCE_ORDER.map((source) => ({
        source,
        label: SOURCE_LABEL[source],
        items: (skills ?? []).filter((s) => s.source === source),
      })),
    [skills],
  );
  // 可键盘选择的条目（跳过分组标题与占位提示）
  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const orderOf = useMemo(
    () => new Map(flat.map((s, i) => [s.name, i])),
    [flat],
  );

  // 每次打开都回到第一项；清单加载完成时同理
  useEffect(() => {
    if (open) setActive(0);
  }, [open, flat]);

  // 点面板/按钮之外的区域收起
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || rootRef.current?.contains(target)) return;
      onOpenChange(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, onOpenChange]);

  // 键盘导航：捕获阶段拦截，避免回车被 Sender 当成「发送」
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!flat.length) return;
        e.preventDefault();
        e.stopPropagation();
        setActive((i) => {
          const next = e.key === "ArrowDown" ? i + 1 : i - 1;
          return Math.max(0, Math.min(flat.length - 1, next));
        });
        return;
      }
      if (e.key === "Enter") {
        if (!flat.length) return;
        e.preventDefault();
        e.stopPropagation();
        onSelect(flat[Math.min(active, flat.length - 1)]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onOpenChange(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, flat, active, onSelect, onOpenChange]);

  // 高亮项保持在可视区内
  useEffect(() => {
    if (!open) return;
    panelRef.current
      ?.querySelector<HTMLElement>(".skill-pop-item.active")
      ?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  return (
    <span className="skill-picker" ref={rootRef}>
      <span
        className={`tb-mode tb-skill${open ? " active" : ""}`}
        title="选择技能"
        onClick={() => onOpenChange(!open)}
      >
        <ReadOutlined />
        <span className="tb-mode-text">技能</span>
        <DownOutlined className="tb-mode-caret" />
      </span>

      {open && (
        <div className="skill-pop" ref={panelRef} role="listbox">
          {skills === null ? (
            <div className="skill-pop-hint">加载中…</div>
          ) : (
            groups.map((group) => {
              if (!group.items.length && group.source !== "custom") return null;
              return (
                <div className="skill-pop-group" key={group.source}>
                  <div className="skill-pop-label">
                    {group.label}
                    {group.items.length > 0 && `（${group.items.length}）`}
                  </div>
                  {group.items.length ? (
                    group.items.map((s) => {
                      const idx = orderOf.get(s.name) ?? -1;
                      return (
                        <div
                          key={s.name}
                          className={`skill-pop-item${idx === active ? " active" : ""}`}
                          role="option"
                          aria-selected={idx === active}
                          onMouseEnter={() => setActive(idx)}
                          onClick={() => onSelect(s)}
                        >
                          <BookOutlined
                            className="skill-pop-icon"
                            style={{ color: SOURCE_COLOR[s.source] }}
                          />
                          <span className="skill-pop-name">{s.name}</span>
                          <span className="skill-pop-desc" title={s.description}>
                            {s.description}
                          </span>
                        </div>
                      );
                    })
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
