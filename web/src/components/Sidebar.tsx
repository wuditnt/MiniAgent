/**
 * 会话侧边栏：自上而下五个区块 ——
 *  ① 品牌区：程序 logo + 标题 + 抽屉折叠图标
 *  ② 大号「新会话」按钮
 *  ③ 功能菜单区：插件 / 自动化任务（暂为占位，点击仅提示）
 *  ④ 工作区：按工作区分组的会话列表（搜索 / 新建 / 改名 / 删除）
 *  ⑤ 底部设置区：「…」弹出菜单（系统配置 / 登录）
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  AppstoreOutlined,
  DeleteOutlined,
  EditOutlined,
  EllipsisOutlined,
  FolderOpenOutlined,
  MenuOutlined,
  MessageOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Dropdown, Empty, Input, Popconfirm, Tooltip, message } from "antd";
import type { SessionItem, WorkspaceItem } from "../types";

interface Props {
  workspaces: WorkspaceItem[];
  sessions: SessionItem[];
  currentId: string;
  /** 侧栏折叠状态（供折叠图标的提示文案使用） */
  collapsed: boolean;
  /** 切换侧栏折叠 / 展开 */
  onToggleCollapse: () => void;
  /** 选中会话；hitIndex 为搜索命中消息的下标（>=0 时前端会滚动定位） */
  onSelect: (sid: string, hitIndex?: number) => void;
  /** 新建对话（用哪个工作区由 App 决定，即输入框上方那个胶囊的选择） */
  onCreateSession: () => void;
  onDeleteSession: (sid: string) => void;
  /** 修改某个会话的标题（在列表项上就地编辑） */
  onRenameSession: (sid: string, title: string) => void;
  onCreateWorkspace: () => void;
  onRenameWorkspace: (w: WorkspaceItem) => void;
  onDeleteWorkspace: (w: WorkspaceItem) => void;
  /** 远程访问时隐藏工作区新建/改名/删除入口（后端同样 403 拦截） */
  remote?: boolean;
  onSearch: (q: string) => void;
  /** 打开「系统配置」弹窗（模型 / 通用） */
  onOpenSettings: () => void;
  /** 登录（占位，暂不实现） */
  onLogin: () => void;
}

/** 把命中的词用 <mark> 包起来（多词 AND，命中片段全部高亮） */
function highlight(text: string, terms: string[]): ReactNode {
  if (!terms.length) return text;
  const lower = text.toLowerCase();
  const ranges: [number, number][] = [];
  for (const t of terms) {
    let from = 0;
    for (;;) {
      const i = lower.indexOf(t, from);
      if (i < 0) break;
      ranges.push([i, i + t.length]);
      from = i + t.length;
    }
  }
  if (!ranges.length) return text;
  ranges.sort((a, b) => a[0] - b[0]);
  // 合并重叠区间，避免嵌套 mark
  const merged: [number, number][] = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  const out: ReactNode[] = [];
  let pos = 0;
  merged.forEach(([s, e], k) => {
    if (s > pos) out.push(text.slice(pos, s));
    out.push(
      <mark key={k} className="search-hit">
        {text.slice(s, e)}
      </mark>
    );
    pos = e;
  });
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

/** 相对时间：刚刚 / N分钟 / N小时 / N天，超过一周显示日期 */
function formatTime(ts: number): string {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}天`;
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export default function Sidebar({
  workspaces,
  sessions,
  currentId,
  collapsed,
  onToggleCollapse,
  onSelect,
  onCreateSession,
  onDeleteSession,
  onRenameSession,
  onCreateWorkspace,
  onRenameWorkspace,
  onDeleteWorkspace,
  remote = false,
  onSearch,
  onOpenSettings,
  onLogin,
}: Props) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [collapsedWs, setCollapsedWs] = useState<Set<string>>(new Set());
  // 会话标题就地编辑：正在编辑的会话 id 与草稿
  const [editId, setEditId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const editDone = useRef(false);

  // 搜索分词：空格分隔，多词 AND；同时作为高亮词表
  const terms = useMemo(
    () => query.trim().toLowerCase().split(/\s+/).filter(Boolean),
    [query]
  );

  // 搜索防抖 300ms，避免每个按键都打后端
  useEffect(() => {
    const t = setTimeout(() => onSearch(query), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  // 按 workspace_id 分组，保持会话原有排序（后端已按更新时间倒序）
  const grouped = useMemo(() => {
    const map = new Map<string, SessionItem[]>();
    for (const s of sessions) {
      const key = s.workspace_id || "default";
      const arr = map.get(key);
      if (arr) arr.push(s);
      else map.set(key, [s]);
    }
    return map;
  }, [sessions]);

  const toggleWs = (wid: string) =>
    setCollapsedWs((prev) => {
      const next = new Set(prev);
      if (next.has(wid)) next.delete(wid);
      else next.add(wid);
      return next;
    });

  /** 进入某个会话标题的就地编辑 */
  const startRename = (s: SessionItem) => {
    editDone.current = false;
    setEditText(s.title);
    setEditId(s.id);
  };

  /** 提交改名（Enter / 失焦）；标题没变或为空则直接关闭 */
  const commitRename = () => {
    if (editDone.current) return;
    editDone.current = true;
    const sid = editId;
    const title = editText.trim();
    setEditId(null);
    const item = sessions.find((x) => x.id === sid);
    if (!sid || !title || (item && item.title === title)) return;
    onRenameSession(sid, title);
  };

  /** 取消改名（Esc） */
  const cancelRename = () => {
    if (editDone.current) return;
    editDone.current = true;
    setEditId(null);
  };

  return (
    <div className="sidebar">
      {/* ① 品牌区：logo + 标题 + 折叠图标 */}
      <div className="sidebar-brand">
        <RobotOutlined className="brand-logo" />
        <span className="brand-name">mini-agent</span>
        <Tooltip title={collapsed ? "展开侧栏" : "收起侧栏"}>
          <MenuOutlined className="sidebar-toggle" onClick={onToggleCollapse} />
        </Tooltip>
      </div>

      {/* ② 新会话大按钮 */}
      <button className="new-session-btn" onClick={onCreateSession}>
        <PlusOutlined />
        <span>新会话</span>
      </button>

      {/* ③④ 可滚动区：功能菜单 + 工作区 */}
      <div className="sidebar-scroll">
        <div className="sidebar-menu">
          <div className="menu-item" onClick={() => message.info("「插件」功能开发中")}>
            <AppstoreOutlined className="menu-item-icon" />
            <span>插件</span>
          </div>
          <div className="menu-item" onClick={() => message.info("「自动化任务」功能开发中")}>
            <ThunderboltOutlined className="menu-item-icon" />
            <span>自动化任务</span>
          </div>
        </div>

        <div className="sidebar-head">
          <span className="sidebar-title">工作区</span>
          <span className="sidebar-actions">
            <Tooltip title="搜索对话">
              <SearchOutlined
                className={`sidebar-icon ${searchOpen ? "on" : ""}`}
                onClick={() => setSearchOpen((v) => !v)}
              />
            </Tooltip>
            <Tooltip title="新建工作区">
              <FolderOpenOutlined
                className="sidebar-icon"
                style={remote ? { display: "none" } : undefined}
                onClick={remote ? undefined : onCreateWorkspace}
              />
            </Tooltip>
          </span>
        </div>

        {searchOpen && (
          <Input
            allowClear
            autoFocus
            size="small"
            prefix={<SearchOutlined style={{ color: "#bbb" }} />}
            placeholder="搜索标题或内容"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ margin: "8px 0" }}
          />
        )}

        <div className="ws-list">
          {workspaces.length === 0 ? (
            <Empty description="暂无工作区" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            workspaces.map((w) => {
              const list = grouped.get(w.id) || [];
              // 搜索中强制展开所有分组，保证命中结果可见
              const wsCollapsed = collapsedWs.has(w.id) && !terms.length;
              return (
                <div className="ws-group" key={w.id}>
                  <div className="ws-head" onClick={() => toggleWs(w.id)}>
                    <span className={`ws-caret ${wsCollapsed ? "collapsed" : ""}`}>▾</span>
                    <FolderOpenOutlined className="ws-icon" />
                    <span className="ws-name" title={w.path}>
                      {w.name}
                    </span>
                    <span className="ws-count">{list.length}</span>
                    <span className="ws-actions" onClick={(e) => e.stopPropagation()}>
                      {!remote && (
                        <>
                          <Tooltip title="改名">
                            <EditOutlined
                              className="ws-action"
                              onClick={() => onRenameWorkspace(w)}
                            />
                          </Tooltip>
                          {w.id !== "default" && (
                            <Popconfirm
                              title="移除这个工作区？"
                              description={
                                w.session_count
                                  ? `组内还有 ${w.session_count} 个对话，需先删除`
                                  : "不会删除任何文件"
                              }
                              okText="移除"
                              cancelText="取消"
                              okButtonProps={{ disabled: !!w.session_count }}
                              onConfirm={() => onDeleteWorkspace(w)}
                            >
                              <DeleteOutlined className="ws-action" />
                            </Popconfirm>
                          )}
                        </>
                      )}
                    </span>
                  </div>

                  {!wsCollapsed && (
                    <div className="ws-sessions">
                      {list.length === 0 ? (
                        <div className="ws-empty">暂无对话</div>
                      ) : (
                        list.map((s) => (
                          <div
                            key={s.id}
                            className={`session-item ${s.id === currentId ? "active" : ""}`}
                            onClick={() => onSelect(s.id, s.hit_index)}
                          >
                            <MessageOutlined className="session-item-icon" />
                            <div className="session-item-body">
                              {editId === s.id ? (
                                <input
                                  className="session-item-input"
                                  autoFocus
                                  maxLength={60}
                                  value={editText}
                                  onChange={(e) => setEditText(e.target.value)}
                                  onClick={(e) => e.stopPropagation()}
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") commitRename();
                                    else if (e.key === "Escape") cancelRename();
                                  }}
                                  onBlur={commitRename}
                                />
                              ) : (
                                <div className="session-item-title">
                                  {highlight(s.title, terms)}
                                </div>
                              )}
                              {s.snippet && editId !== s.id ? (
                                <div className="session-item-snippet">
                                  {highlight(s.snippet, terms)}
                                </div>
                              ) : null}
                            </div>
                            <span className="session-item-time">{formatTime(s.updated_at)}</span>
                            {editId !== s.id && (
                              <Tooltip title="重命名">
                                <EditOutlined
                                  className="session-item-edit"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    startRename(s);
                                  }}
                                />
                              </Tooltip>
                            )}
                            <Popconfirm
                              title="删除这个对话？"
                              okText="删除"
                              cancelText="取消"
                              onConfirm={(e) => {
                                e?.stopPropagation();
                                onDeleteSession(s.id);
                              }}
                              onPopupClick={(e) => e.stopPropagation()}
                            >
                              <Tooltip title="删除">
                                <DeleteOutlined
                                  className="session-item-delete"
                                  onClick={(e) => e.stopPropagation()}
                                />
                              </Tooltip>
                            </Popconfirm>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* ⑤ 底部设置区 */}
      <div className="sidebar-foot">
        <Dropdown
          trigger={["click"]}
          placement="topLeft"
          menu={{
            items: [
              { key: "settings", label: "系统配置", icon: <SettingOutlined /> },
              { key: "login", label: "登录", icon: <UserOutlined /> },
            ],
            onClick: ({ key }) => {
              if (key === "settings") onOpenSettings();
              else onLogin();
            },
          }}
        >
          <div className="foot-more">
            <EllipsisOutlined />
          </div>
        </Dropdown>
      </div>
    </div>
  );
}
