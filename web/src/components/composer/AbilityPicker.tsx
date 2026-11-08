/**
 * 输入框工具栏的「能力」菜单：技能 + 连接器。
 *
 * 结构与 SkillPicker 相同：面板绝对定位挂在 .composer-slot 上，宽度跟随输入框。
 * - 技能页签：保持原有单选动作（点一下 / 回车即插入并关闭）；
 * - 连接器页签：多选清单，勾选立即写入本会话（并同步全局默认），不需要「确定」。
 *
 * 连接器只在智能体模式下挂工具，因此整个菜单仍由 Chat 在 agent 模式渲染。
 * 键盘导航只作用于技能页签，连接器页签只用鼠标/空格，避免回车误发消息。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AppstoreOutlined,
  BookOutlined,
  DownOutlined,
  LinkOutlined,
} from "@ant-design/icons";
import { message } from "antd";
import { fetchConnectors, setSessionConnectors } from "../../api";
import type { ConnectorsData, SkillItem } from "../../types";

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

type Tab = "skills" | "connectors";

export default function AbilityPicker({
  skills,
  open,
  searching = false,
  onOpenChange,
  onSelect,
  sessionId,
  connectors,
  draft = false,
  onConnectorsChange,
}: {
  skills: SkillItem[] | null;
  open: boolean;
  /** 正在按 "/查询词" 过滤：不显示「暂无自定义技能」这类空态 */
  searching?: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: (item: SkillItem) => void;
  /** 当前会话 id：连接器勾选写在这个会话上 */
  sessionId: string;
  /** 本会话显式勾选；null/缺省 = 跟全局默认 */
  connectors: string[] | null;
  /** 草稿会话尚未落盘：只改前端状态，首条消息时随请求保存 */
  draft?: boolean;
  onConnectorsChange: (list: string[] | null) => void;
}) {
  const rootRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState<Tab>("skills");
  // 键盘高亮项（仅技能页签）
  const [active, setActive] = useState(0);
  const [data, setData] = useState<ConnectorsData | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

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

  /** 打开时拉一次连接器清单（勾选状态与工具数都要最新） */
  const loadConnectors = () => {
    setLoading(true);
    fetchConnectors()
      .then(setData)
      .catch((e) => message.error(`加载连接器失败：${String(e?.message || e)}`))
      .finally(() => setLoading(false));
  };

  // 每次打开都回到第一项；清单加载完成时同理
  useEffect(() => {
    if (open) setActive(0);
  }, [open, flat]);

  // "/" 直接搜索技能时，强制回到技能页签
  useEffect(() => {
    if (open && searching) setTab("skills");
  }, [open, searching]);

  // 打开面板时刷新连接器状态
  useEffect(() => {
    if (!open) return;
    loadConnectors();
  }, [open]);

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

  // 键盘导航：捕获阶段拦截，避免回车被 Sender 当成「发送」（仅技能页签）
  useEffect(() => {
    if (!open || tab !== "skills") return;
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
  }, [open, tab, flat, active, onSelect, onOpenChange]);

  // 关闭时也允许 Esc（连接器页签没有键盘导航，但收起要一致）
  useEffect(() => {
    if (!open || tab !== "connectors") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.isComposing) {
        e.preventDefault();
        e.stopPropagation();
        onOpenChange(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, tab, onOpenChange]);

  // 高亮项保持在可视区内
  useEffect(() => {
    if (!open || tab !== "skills") return;
    panelRef.current
      ?.querySelector<HTMLElement>(".skill-pop-item.active")
      ?.scrollIntoView({ block: "nearest" });
  }, [open, tab, active]);

  const enabledNames = useMemo(
    () => (data?.connectors ?? []).filter((c) => c.enabled).map((c) => c.name),
    [data],
  );
  /** 当前生效的勾选：会话显式选择优先，否则跟全局默认（默认也没设置就用全部已启用） */
  const checked = useMemo(() => {
    if (connectors !== null) return new Set(connectors);
    const fallback = data?.defaults ?? enabledNames;
    return new Set(fallback);
  }, [connectors, data, enabledNames]);

  /** 勾选/取消：立即写会话；本机管理员同时同步全局默认（后端判断） */
  const toggleConnector = async (name: string) => {
    if (!data || saving) return;
    const nextSet = new Set(checked);
    if (nextSet.has(name)) nextSet.delete(name);
    else nextSet.add(name);
    // 按配置顺序输出，避免每次勾选顺序抖动
    const next = data.connectors.filter((c) => nextSet.has(c.name)).map((c) => c.name);
    const prev = connectors ?? null;

    onConnectorsChange(next);
    if (draft) return; // 草稿会话：首条消息时随请求落盘

    setSaving(true);
    try {
      const r = await setSessionConnectors(sessionId, next);
      setData((d) => (d ? { ...d, defaults: r.defaults } : d));
    } catch (e) {
      onConnectorsChange(prev);
      message.error(`保存连接器选择失败：${String((e as Error)?.message || e)}`);
    } finally {
      setSaving(false);
    }
  };

  const total = data?.connectors.length ?? 0;
  const selectedCount = (data?.connectors ?? []).filter((c) => checked.has(c.name)).length;

  return (
    <span className="skill-picker" ref={rootRef}>
      <span
        className={`tb-mode tb-skill${open ? " active" : ""}`}
        title="能力：技能与连接器"
        onClick={() => onOpenChange(!open)}
      >
        <AppstoreOutlined />
        <span className="tb-mode-text">能力</span>
        <DownOutlined className="tb-mode-caret" />
      </span>

      {open && (
        <div className="skill-pop ability-pop" ref={panelRef}>
          <div className="ability-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "skills"}
              className={`ability-tab${tab === "skills" ? " active" : ""}`}
              onClick={() => setTab("skills")}
            >
              技能
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "connectors"}
              className={`ability-tab${tab === "connectors" ? " active" : ""}`}
              onClick={() => {
                setTab("connectors");
                loadConnectors();
              }}
            >
              连接器
              {total > 0 && (
                <span className="ability-tab-count">
                  {selectedCount}/{total}
                </span>
              )}
            </button>
          </div>

          {tab === "skills" ? (
            <div className="ability-body" role="listbox">
              {skills === null ? (
                <div className="skill-pop-hint">加载中…</div>
              ) : (
                groups.map((group) => {
                  // 搜索时隐藏空分组；平时保留「暂无自定义技能」提示
                  if (!group.items.length && (searching || group.source !== "custom")) return null;
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
          ) : (
            <div className="ability-body connector-pop-body">
              {loading && !data ? (
                <div className="skill-pop-hint">加载中…</div>
              ) : !data ? (
                <div className="skill-pop-hint">加载失败</div>
              ) : !data.mcp_enabled ? (
                <div className="skill-pop-hint">MCP 连接器未启用，请检查启动参数</div>
              ) : !data.connectors.length ? (
                <div className="skill-pop-hint">
                  还没有配置连接器，请到左侧「连接」页添加
                </div>
              ) : (
                <>
                  {data.connectors.map((c) => {
                    const on = checked.has(c.name);
                    return (
                      <div
                        key={c.name}
                        className={`connector-pop-item${!c.enabled ? " disabled" : ""}`}
                        role="checkbox"
                        aria-checked={on}
                        aria-disabled={!c.enabled}
                        onClick={() => c.enabled && toggleConnector(c.name)}
                      >
                        <span
                          className={`connector-pop-check${on ? " on" : ""}`}
                          aria-hidden
                        />
                        <LinkOutlined className="skill-pop-icon connector-pop-icon" />
                        <span className="connector-pop-main">
                          <span className="skill-pop-name">
                            {c.display_name || c.name}
                          </span>
                          <span className="connector-pop-desc">
                            {c.enabled
                              ? `${c.tool_count} 个工具`
                              : "已禁用（去「连接」页启用）"}
                          </span>
                        </span>
                        {c.enabled && c.status === "failed" && (
                          <span className="connector-pop-badge err">失败</span>
                        )}
                      </div>
                    );
                  })}
                  <div className="skill-pop-hint connector-pop-tip">
                    勾选只影响本会话，并同步为全局默认
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </span>
  );
}
