/**
 * 应用编排：模式/联网开关、消息流、SSE 消费、危险确认都在这里。
 * Chat / ModeBar / WelcomePane / ConfirmModal / ToolChain 是纯展示组件。
 */
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { DownOutlined, DeleteOutlined, ExportOutlined, BarChartOutlined, LayoutOutlined, MenuOutlined, SettingOutlined } from "@ant-design/icons";
import { Dropdown, Input, message, Modal, Tooltip } from "antd";
import {
  activateModel,
  chatStream,
  createSession,
  createWorkspace,
  deleteSession,
  deleteWorkspace,
  fetchHistory,
  fetchModels,
  fetchSessions,
  fetchState,
  fetchWorkspaces,
  pickFolder,
  postConfirm,
  postStop,
  truncateSession,
  updateSession,
  updateWorkspace,
} from "./api";
import type {
  AgentState,
  ApprovalMode,
  ChatItem,
  ConfirmInfo,
  Mode,
  ModelProvider,
  SessionItem,
  SSEEvent,
  WorkspaceItem,
} from "./types";
import Chat from "./components/Chat";
import ModeBar from "./components/ModeBar";
import ConfirmModal from "./components/ConfirmModal";
import Sidebar from "./components/Sidebar";
import WorkspaceModal from "./components/WorkspaceModal";
import SettingsModal from "./components/SettingsModal";
import type { SettingsTab } from "./components/SettingsModal";
import Workbench from "./components/Workbench";

const SESSION_KEY = "miniagent.session";
const MODE_KEY = "miniagent.mode";
const WEB_KEY = "miniagent.web";
const WS_KEY = "miniagent.workspace";
const APPROVAL_KEY = "miniagent.approval";

/** 浏览器侧会话 id：localStorage 记忆，刷新后恢复同一会话历史 */
function loadSession(): string {
  let sid = localStorage.getItem(SESSION_KEY) || "";
  if (!sid) {
    sid =
      typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(SESSION_KEY, sid);
  }
  return sid;
}

export default function App() {
  const [session, setSession] = useState<string>(loadSession);
  const [mode, setMode] = useState<Mode>(() =>
    localStorage.getItem(MODE_KEY) === "chat" ? "chat" : "agent"
  );
  const [web, setWeb] = useState<boolean>(() => localStorage.getItem(WEB_KEY) === "1");
  const [approval, setApproval] = useState<ApprovalMode>(() => {
    const v = localStorage.getItem(APPROVAL_KEY);
    return v === "auto" || v === "full" ? v : "ask";
  });
  const [state, setState] = useState<AgentState | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmInfo | null>(null);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  // 顶栏模型单选菜单的数据源（/api/models）
  const [models, setModels] = useState<ModelProvider[]>([]);
  const [wsModal, setWsModal] = useState<{
    mode: "create" | "edit";
    item?: WorkspaceItem;
    pickedPath?: string;
  } | null>(null);
  // 窄屏（≤768px）默认收起侧栏，给聊天区留足空间
  const [collapsed, setCollapsed] = useState(() => typeof window !== "undefined" && window.innerWidth <= 768);

  // 屏幕宽度变化时自动收起/展开侧栏
  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth <= 768 && !collapsed) setCollapsed(true);
      else if (window.innerWidth > 768 && collapsed) setCollapsed(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [collapsed]);
  // 顶栏会话标题：就地编辑状态
  const [titleEditing, setTitleEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  // 防止 Enter 提交后紧接着的 blur 二次提交 / 取消标志
  const titleDone = useRef(false);
  // 当前（或下一个新）对话使用的工作区；没选过就是默认工作区
  const [ws, setWs] = useState<string>(() => localStorage.getItem(WS_KEY) || "default");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [wbFullscreen, setWbFullscreen] = useState(false);
  const [statsOpen, setStatsOpen] = useState(false);
  // 搜索命中后要定位闪烁的消息 id
  const [focus, setFocus] = useState<{ id: string; nonce: number } | null>(null);
  const pendingHit = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // 开关状态持久化（逐消息生效：send 时读取当前 state，不依赖这里的时序）
  useEffect(() => {
    localStorage.setItem(MODE_KEY, mode);
  }, [mode]);
  useEffect(() => {
    localStorage.setItem(WEB_KEY, web ? "1" : "0");
  }, [web]);
  useEffect(() => {
    localStorage.setItem(WS_KEY, ws);
  }, [ws]);
  useEffect(() => {
    localStorage.setItem(APPROVAL_KEY, approval);
  }, [approval]);

  /** 会话列表刷新（支持搜索词，跨全部工作区） */
  const refreshSessions = (q: string = "") => {
    fetchSessions(q).then(setSessions).catch(() => {});
  };

  /** 工作区列表刷新（含每个工作区下的会话数） */
  const refreshWorkspaces = () => {
    fetchWorkspaces().then(setWorkspaces).catch(() => {});
  };

  /** 模型列表刷新（顶栏模型单选菜单） */
  const refreshModels = () => {
    fetchModels()
      .then((m) => setModels(m.providers))
      .catch(() => {});
  };

  /** 会话 + 工作区一起刷新（新建/删除后调用，保持分组与计数一致） */
  const refreshAll = () => {
    refreshSessions();
    refreshWorkspaces();
  };

  // 首次加载：元信息 + 历史消息 + 会话/工作区/模型列表；卸载时中断在途请求
  useEffect(() => {
    fetchState()
      .then(setState)
      .catch(() => setState(null));
    refreshAll();
    refreshModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切换会话时重新加载历史
  useEffect(() => {
    localStorage.setItem(SESSION_KEY, session);
    setItems([]);
    fetchHistory(session)
      .then((h) => {
        setItems(
          h.messages.map((m, i) => ({
            id: `h-${i}`,
            role: m.role === "user" ? "user" : "assistant",
            content: m.content,
            status: "done" as const,
            // 落盘时一并保存的用量/耗时，回放时原样恢复脚注
            usage: m.usage,
            sec: m.sec,
            // 附件元数据：发送时写入，历史回放原样展示
            attachments: m.attachments,
          }))
        );
        // 若本次是从搜索结果点进来的，定位到命中那条消息并闪烁
        const hit = pendingHit.current;
        pendingHit.current = null;
        if (hit != null && hit >= 0 && hit < h.messages.length) {
          jumpToHit(hit);
        }
      })
      .catch(() => {});
  }, [session]);
  useEffect(() => () => abortRef.current?.abort(), []);

  // 快捷键
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "n") {
        e.preventDefault();
        handleNewSession();
      } else if (mod && e.key === "e") {
        e.preventDefault();
        handleExportMarkdown();
      } else if (mod && e.shiftKey && (e.key === "Delete" || e.key === "Backspace")) {
        e.preventDefault();
        handleClearSession();
      } else if (mod && e.key === "/") {
        e.preventDefault();
        message.info(
          <div style={{ textAlign: "left" }}>
            <div><b>Ctrl/⌘ + N</b> 新建会话</div>
            <div><b>Ctrl/⌘ + E</b> 导出 Markdown</div>
            <div><b>Ctrl/⌘ + Shift + Delete</b> 清空会话</div>
            <div><b>Enter</b> 发送 / <b>Shift+Enter</b> 换行</div>
          </div>,
          5
        );
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // 切换会话时同步工作区：只对「已经开始的会话」跟它自己的归属走；
  // 空会话保留用户在胶囊里的当前选择（否则刚选完就被会话默认值覆盖回去）
  useEffect(() => {
    const cur = sessions.find((s) => s.id === session);
    if (cur && cur.message_count > 0 && cur.workspace_id) setWs(cur.workspace_id);
  }, [session, sessions]);

  const patchAssistant = (id: string, patch: (it: ChatItem) => ChatItem) =>
    setItems((prev) => prev.map((it) => (it.id === id ? patch(it) : it)));

  /** 终态兜底：把还没等到 tool_end 的 pending 工具节点标记为失败 */
  const finalizeTools = (it: ChatItem): ChatItem => ({
    ...it,
    tools: (it.tools || []).map((t) =>
      t.ok === undefined ? { ...t, ok: false, result: "[cancelled] 任务未完成" } : t
    ),
  });

  /** 发送一条消息：乐观插入两条气泡，然后消费 SSE 事件流增量更新 */
  const send = (
    text: string,
    attachments?: import("./types").Attachment[],
    skipUserItem?: boolean
  ) => {
    const msg = text.trim();
    if (!msg && !(attachments && attachments.length)) return;
    if (busy) return;
    const asstId = `a-${Date.now()}`;
    const userItem: ChatItem = {
      id: `u-${Date.now()}`,
      role: "user",
      content: msg,
      status: "done",
      attachments,
    };
    const asstItem: ChatItem = {
      id: asstId,
      role: "assistant",
      content: "",
      status: "streaming",
      mode,
    };
    if (skipUserItem) {
      // 重新生成场景：user 消息已保留在列表里，只需追加 assistant 气泡
      setItems((prev) => [...prev, asstItem]);
    } else {
      setItems((prev) => [...prev, userItem, asstItem]);
    }
    setBusy(true);
    const ac = new AbortController();
    abortRef.current = ac;

    chatStream(
      { message: msg, mode, web, approval, session, workspace: ws, attachments },
      (ev: SSEEvent) => {
        switch (ev.type) {
          case "delta":
            patchAssistant(asstId, (it) =>
              ev.kind === "reasoning"
                ? { ...it, reasoning: (it.reasoning || "") + ev.text }
                : { ...it, content: it.content + ev.text }
            );
            break;
          case "tool_start":
            patchAssistant(asstId, (it) => ({
              ...it,
              tools: [...(it.tools || []), { id: ev.id, name: ev.name, args: ev.args }],
            }));
            break;
          case "tool_end":
            patchAssistant(asstId, (it) => ({
              ...it,
              tools: (it.tools || []).map((t) =>
                t.id === ev.id ? { ...t, ok: ev.ok, sec: ev.sec, result: ev.result } : t
              ),
            }));
            break;
          case "confirm_request":
            setConfirm({ id: ev.id, tool: ev.tool, args: ev.args });
            break;
          case "notice":
            setItems((prev) => [
              ...prev,
              {
                id: `n-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`,
                role: "notice",
                content: ev.text,
                status: "done",
              },
            ]);
            break;
          case "done":
            // answer 是服务端合并后的最终回答，比逐帧拼接更可靠
            patchAssistant(asstId, (it) => ({
              ...it,
              status: "done",
              content: ev.answer || it.content,
              sec: ev.sec,
              usage: ev.usage,
            }));
            break;
          case "error":
            patchAssistant(asstId, (it) =>
              finalizeTools({ ...it, status: "error", error: ev.message, usage: ev.usage })
            );
            setConfirm(null);
            break;
          case "cancelled":
            // 串行路径取消时 agent 不发 tool_end：这里把挂着的 pending 节点收尾，避免永远转圈
            patchAssistant(asstId, (it) =>
              finalizeTools({ ...it, status: "cancelled", reason: ev.reason, usage: ev.usage })
            );
            setConfirm(null);
            break;
          default:
            break; // turn / llm_done：前端无需单独处理
        }
      },
      ac.signal
    )
      .catch((err: unknown) => {
        if (ac.signal.aborted) return; // 页面卸载导致的 abort，无需展示
        const msg = err instanceof Error ? err.message : String(err);
        patchAssistant(asstId, (it) => ({ ...it, status: "error", error: msg }));
      })
      .finally(() => {
        setBusy(false);
        refreshAll(); // 对话结束后刷新列表（标题/时间/计数可能变了）
      });
  };

  /** 停止：只触发服务端取消令牌；cancelled 终态事件随后由流送达 */
  const stop = () => {
    postStop(session).catch(() => {});
  };

  /** 重新生成：截掉该 assistant 消息及之后的内容，重发前一条 user 消息 */
  const handleRegenerate = (msgId: string) => {
    const idx = items.findIndex((it) => it.id === msgId);
    if (idx < 0 || items[idx].role !== "assistant") return;
    // 找前一条 user 消息（重新生成 = 保留 user、去掉 assistant 回答后重发）
    let userIdx = -1;
    for (let i = idx - 1; i >= 0; i--) {
      if (items[i].role === "user") { userIdx = i; break; }
    }
    if (userIdx < 0) return;
    const userMsg = items[userIdx];
    // keep = 该 user 消息及其之前所有非 notice 消息（后端索引不含 notice）
    const keep = items.slice(0, userIdx + 1).filter((it) => it.role !== "notice").length;
    truncateSession(session, keep)
      .then(() => {
        setItems((prev) => prev.slice(0, userIdx + 1));
        send(userMsg.content, userMsg.attachments, true);
      })
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "重新生成失败"));
  };

  /** 编辑重发：截掉该 user 消息及之后的内容，原文由 Chat 回填输入框 */
  const handleEditResend = (msgId: string) => {
    const idx = items.findIndex((it) => it.id === msgId);
    if (idx < 0 || items[idx].role !== "user") return;
    // keep = 该 user 消息之前所有非 notice 消息（不含这条 user 本身）
    const keep = items.slice(0, idx).filter((it) => it.role !== "notice").length;
    truncateSession(session, keep)
      .then(() => {
        setItems((prev) => prev.slice(0, idx));
      })
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "编辑重发失败"));
  };

  /** 新建对话：在当前选中的工作区下创建空会话 → 切换过去 */
  const handleNewSession = () => {
    createSession(ws)
      .then((s) => setSession(s.id))
      .catch(() => {});
  };

  /** 触发一次「定位到第 idx 条消息并闪烁」 */
  const jumpToHit = (idx: number) => {
    setFocus({ id: `h-${idx}`, nonce: Date.now() });
  };

  // 定位请求：等这一帧渲染完成后再查 DOM，避免消息还没挂上就找不到元素
  useEffect(() => {
    if (!focus) return;
    const timer = setTimeout(() => {
      const el = document.getElementById(`msg-${focus.id}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        const target = (el.closest(".ant-bubble") || el) as HTMLElement;
        target.classList.add("flash");
        setTimeout(() => target.classList.remove("flash"), 2000);
      }
      setFocus(null); // 用完即清，避免影响后续「跟随滚动到底部」
    }, 80);
    return () => clearTimeout(timer);
  }, [focus]);

  /** 切换对话；hitIndex 来自搜索结果，用于点进去后定位闪烁 */
  const handleSelectSession = (sid: string, hitIndex?: number) => {
    const hasHit = typeof hitIndex === "number" && hitIndex >= 0;
    if (sid === session) {
      // 已停在该会话：历史就在屏上，直接定位即可
      if (hasHit) jumpToHit(hitIndex as number);
      return;
    }
    pendingHit.current = hasHit ? (hitIndex as number) : null;
    setSession(sid);
  };

  /** 删除对话：删当前则自动切到列表里的下一个 */
  const handleDeleteSession = (sid: string) => {
    deleteSession(sid)
      .then(() => {
        if (sid === session) {
          const rest = sessions.filter((s) => s.id !== sid);
          setSession(rest[0]?.id || "");
          if (!rest.length) {
            // 删完了，新建一个兜底（落在当前选中的工作区）
            createSession(ws)
              .then((s) => setSession(s.id))
              .catch(() => {});
          }
        }
        refreshAll();
      })
      .catch(() => {});
  };

  /** 修改会话标题（顶栏与侧栏共用）；成功后刷新列表同步新标题 */
  const handleRenameSession = (sid: string, title: string) => {
    const t = title.trim();
    if (!sid || !t) return;
    updateSession(sid, t)
      .then(() => refreshSessions())
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "修改标题失败"));
  };

  // ---------------------------------------------------------- 工作区

  /** 新建工作区：先弹本机原生文件夹选择框，选中后名称自动取文件夹名 */
  const handleCreateWorkspace = () => {
    pickFolder()
      .then((path) => {
        if (!path) return; // 用户取消选择，不弹确认窗
        setWsModal({ mode: "create", pickedPath: path });
      })
      .catch((e: unknown) =>
        message.error(e instanceof Error ? e.message : "无法打开文件夹选择框")
      );
  };

  /** 打开「编辑工作区」弹窗（改名 / 改路径） */
  const handleRenameWorkspace = (w: WorkspaceItem) => setWsModal({ mode: "edit", item: w });

  /** 弹窗提交：新建或更新，失败时把错误抛回弹窗内联展示 */
  const handleWorkspaceSubmit = async (name: string, path: string) => {
    if (wsModal?.mode === "edit" && wsModal.item) {
      await updateWorkspace(wsModal.item.id, { name, path });
      message.success("工作区已更新");
    } else {
      await createWorkspace(name, path);
      message.success("工作区已创建");
    }
    setWsModal(null);
    refreshAll();
  };

  /** 移除工作区（默认工作区 / 非空工作区会被后端拒绝） */
  const handleDeleteWorkspace = (w: WorkspaceItem) => {
    deleteWorkspace(w.id)
      .then(() => {
        message.success("工作区已移除");
        refreshAll();
      })
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "移除失败"));
  };

  /** 删除消息：截掉该条及之后的所有内容（含该条） */
  const handleDeleteMessage = (msgId: string) => {
    const idx = items.findIndex((it) => it.id === msgId);
    if (idx < 0) return;
    const keep = items.slice(0, idx).filter((it) => it.role !== "notice").length;
    truncateSession(session, keep)
      .then(() => {
        setItems((prev) => prev.slice(0, idx));
      })
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "删除失败"));
  };

  /** 清空会话：截断到 0 */
  const handleClearSession = () => {
    truncateSession(session, 0)
      .then(() => setItems([]))
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "清空失败"));
  };

  /** 导出当前会话为 Markdown 文件 */
  const handleExportMarkdown = () => {
    if (!items.length) {
      message.info("当前会话没有消息可导出");
      return;
    }
    const title = currentSession?.title || "新对话";
    const now = new Date().toLocaleString("zh-CN");
    let md = `# ${title}\n\n导出时间：${now}\n\n---\n\n`;
    items.forEach((it) => {
      if (it.role === "notice") return;
      if (it.role === "user") {
        md += `## User\n\n${it.content}\n\n`;
        if (it.attachments?.length) {
          md += `> 附件：${it.attachments.map((a) => a.name).join("、")}\n\n`;
        }
      } else {
        md += `## Assistant\n\n${it.content}\n\n`;
        if (it.tools?.length) {
          md += `> 工具调用：${it.tools.map((t) => t.name).join("、")}\n\n`;
        }
      }
      md += "---\n\n";
    });
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${title.replace(/[\\/:*?"<>|]/g, "_")}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // 当前会话（可能还没落盘，所以找不到就是 undefined）
  const currentSession = sessions.find((s) => s.id === session);
  // 顶栏模型菜单里当前激活的那一项
  const activeModelId = models.find((m) => m.is_active)?.id;
  // 上下文水位：所有非 notice 消息的文本总长度（近似值，含 reasoning/tools）
  const contextUsage = items.reduce((sum, it) => {
    if (it.role === "notice") return sum;
    let s = it.content.length + (it.reasoning?.length || 0);
    if (it.tools) {
      s += it.tools.reduce((ts, t) => ts + (t.args ? JSON.stringify(t.args).length : 0) + (t.result?.length || 0), 0);
    }
    return sum + s;
  }, 0);
  const contextLimit = state?.max_context_chars ?? 200000;

  /** 顶栏标题：提交（Enter / 失焦）；未改动则直接关闭 */
  const commitTitle = () => {
    if (titleDone.current) return;
    titleDone.current = true;
    setTitleEditing(false);
    const t = titleDraft.trim();
    if (!currentSession || !t || t === currentSession.title) return;
    handleRenameSession(session, t);
  };

  /** 顶栏标题：取消（Esc） */
  const cancelTitle = () => {
    if (titleDone.current) return;
    titleDone.current = true;
    setTitleEditing(false);
  };

  return (
    <div
      className={`app ${wbFullscreen ? "wb-fullscreen" : ""}`}
      style={{ "--sidebar-w": collapsed ? "0px" : "260px" } as CSSProperties}
    >
      <aside className={`sidebar-fixed ${collapsed ? "collapsed" : ""}`}>
        <Sidebar
          workspaces={workspaces}
          sessions={sessions}
          currentId={session}
          collapsed={collapsed}
          remote={!!state?.remote}
          onToggleCollapse={() => setCollapsed((c) => !c)}
          onSelect={handleSelectSession}
          onCreateSession={handleNewSession}
          onDeleteSession={handleDeleteSession}
          onCreateWorkspace={handleCreateWorkspace}
          onRenameWorkspace={handleRenameWorkspace}
          onDeleteWorkspace={handleDeleteWorkspace}
          onSearch={(q) => refreshSessions(q)}
          onRenameSession={handleRenameSession}
          onOpenSettings={() => {
            setSettingsTab("general");
            setSettingsOpen(true);
          }}
          onLogin={() => message.info("「登录」功能开发中")}
        />
      </aside>

      <div className="main">
        <header className="app-header">
          <div className="header-left">
            {collapsed && (
              <Tooltip title="展开侧栏">
                <MenuOutlined className="expand-btn" onClick={() => setCollapsed(false)} />
              </Tooltip>
            )}
            {titleEditing ? (
              <Input
                className="header-title-input"
                size="small"
                autoFocus
                maxLength={60}
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onPressEnter={commitTitle}
                onBlur={commitTitle}
                onKeyDown={(e) => {
                  if (e.key === "Escape") cancelTitle();
                }}
              />
            ) : (
              <Tooltip title={currentSession ? "点击修改标题" : undefined}>
                <span
                  className="header-title"
                  onClick={() => {
                    if (!currentSession) return;
                    titleDone.current = false;
                    setTitleDraft(currentSession.title || "");
                    setTitleEditing(true);
                  }}
                >
                  {currentSession?.title || "新对话"}
                </span>
              </Tooltip>
            )}
          </div>

          <div className="header-right">
            {/* 用量统计 */}
            <Tooltip title="用量统计">
              <span className="tb-icon-btn" onClick={() => setStatsOpen(true)}>
                <BarChartOutlined />
              </span>
            </Tooltip>
            {/* 清空会话 */}
            <Tooltip title="清空会话">
              <span className="tb-icon-btn" onClick={handleClearSession}>
                <DeleteOutlined />
              </span>
            </Tooltip>
            {/* 导出当前会话为 Markdown */}
            <Tooltip title="导出为 Markdown">
              <span className="tb-icon-btn" onClick={handleExportMarkdown}>
                <ExportOutlined />
              </span>
            </Tooltip>
            {/* 模型单选菜单：选中即切换激活模型，末项固定为「管理模型」 */}
            <Dropdown
              trigger={["click"]}
              placement="bottomRight"
              menu={{
                selectable: true,
                selectedKeys: activeModelId ? [activeModelId] : [],
                items: [
                  ...models.map((m) => ({
                    key: m.id,
                    label: <span className="model-option">{m.name || m.model}</span>,
                  })),
                  { type: "divider" as const },
                  { key: "__manage__", label: "管理模型", icon: <SettingOutlined /> },
                ],
                onClick: ({ key }) => {
                  if (key === "__manage__") {
                    setSettingsTab("model");
                    setSettingsOpen(true);
                    return;
                  }
                  activateModel(key)
                    .then(() => {
                      refreshModels();
                      fetchState().then(setState).catch(() => {});
                    })
                    .catch((e: unknown) =>
                      message.error(e instanceof Error ? e.message : "切换模型失败")
                    );
                },
              }}
            >
              <span className="model-chip">
                {state?.model || "连接中…"}
                <DownOutlined className="model-chip-caret" />
              </span>
            </Dropdown>
            <ModeBar web={web} state={state} onWeb={setWeb} />
            {/* 工作台抽屉开关 */}
            <Tooltip title={workbenchOpen ? "收起工作区" : "工作区文件"}>
              <span
                className={`tb-icon-btn ${workbenchOpen ? "active" : ""}`}
                onClick={() => {
                  if (workbenchOpen) {
                    setWorkbenchOpen(false);
                    setWbFullscreen(false);
                  } else {
                    setWorkbenchOpen(true);
                  }
                }}
              >
                <LayoutOutlined />
              </span>
            </Tooltip>
          </div>
        </header>

        <Chat
          items={items}
          busy={busy}
          mode={mode}
          onMode={setMode}
          approval={approval}
          onApproval={setApproval}
          onSend={send}
          onStop={stop}
          focusId={focus?.id ?? null}
          workspaces={workspaces}
          workspaceId={ws}
          onWorkspaceChange={(id) => setWs(id)}
          workspaceLocked={!!currentSession && currentSession.message_count > 0}
          onRegenerate={busy ? undefined : handleRegenerate}
          onEditResend={busy ? undefined : handleEditResend}
          onDelete={busy ? undefined : handleDeleteMessage}
          contextUsage={contextUsage}
          contextLimit={contextLimit}
        />

        <ConfirmModal
          info={confirm}
          onAnswer={(approve) => {
            if (!confirm) return;
            postConfirm(confirm.id, approve, session).catch(() => {});
            setConfirm(null);
          }}
        />

        <WorkspaceModal
          open={wsModal !== null}
          mode={wsModal?.mode || "create"}
          initial={wsModal?.item || null}
          pickedPath={wsModal?.pickedPath ?? null}
          onCancel={() => setWsModal(null)}
          onSubmit={handleWorkspaceSubmit}
        />

        <SettingsModal
          open={settingsOpen}
          tab={settingsTab}
          workspaceId={ws}
          onTab={setSettingsTab}
          onClose={() => setSettingsOpen(false)}
          onChanged={() => {
            fetchState().then(setState).catch(() => {});
            refreshModels();
          }}
        />

        {/* 用量统计面板 */}
        <Modal
          open={statsOpen}
          title="用量统计"
          width={640}
          onCancel={() => setStatsOpen(false)}
          footer={null}
        >
          {(() => {
            const valid = items.filter((it) => it.role !== "notice");
            const userCount = valid.filter((it) => it.role === "user").length;
            const asstCount = valid.filter((it) => it.role === "assistant").length;
            let totalPrompt = 0, totalCompletion = 0, totalTokens = 0, totalSec = 0, totalLlm = 0, totalTools = 0;
            valid.forEach((it) => {
              if (it.usage) {
                totalPrompt += it.usage.prompt_tokens;
                totalCompletion += it.usage.completion_tokens;
                totalTokens += it.usage.total_tokens;
                totalLlm += it.usage.llm_calls;
                totalTools += it.usage.tool_calls;
              }
              if (it.sec) totalSec += it.sec;
            });
            const avgTokens = asstCount > 0 ? Math.round(totalTokens / asstCount) : 0;
            return (
              <div>
                <div className="stats-grid">
                  <div className="stat-card">
                    <div className="stat-value">{valid.length}</div>
                    <div className="stat-label">总消息数</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-value">{userCount} / {asstCount}</div>
                    <div className="stat-label">用户 / 助手</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-value">{totalTokens.toLocaleString()}</div>
                    <div className="stat-label">总 tokens</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-value">{avgTokens}</div>
                    <div className="stat-label">平均每轮</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-value">{totalSec.toFixed(1)}s</div>
                    <div className="stat-label">总耗时</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-value">{totalLlm} / {totalTools}</div>
                    <div className="stat-label">LLM / 工具调用</div>
                  </div>
                </div>
                <div className="stats-detail">
                  <div className="stats-detail-title">逐条明细</div>
                  {valid.map((it, i) => (
                    <div className="stats-row" key={it.id}>
                      <span className="stats-row-idx">{i + 1}</span>
                      <span className="stats-row-role">{it.role === "user" ? "用户" : "助手"}</span>
                      <span className="stats-row-content" title={it.content}>
                        {it.content.slice(0, 40)}{it.content.length > 40 ? "…" : ""}
                      </span>
                      <span className="stats-row-usage">
                        {it.usage ? `${it.usage.total_tokens} tok` : "-"}
                      </span>
                      <span className="stats-row-sec">{it.sec ? `${it.sec}s` : "-"}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}
        </Modal>
      </div>

      <Workbench
        open={workbenchOpen}
        workspaceId={ws}
        onClose={() => {
          setWorkbenchOpen(false);
          setWbFullscreen(false);
        }}
        onFullscreenChange={setWbFullscreen}
      />
    </div>
  );
}
