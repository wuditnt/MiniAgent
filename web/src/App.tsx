/**
 * 应用编排：模式/联网开关、消息流、SSE 消费、危险确认都在这里。
 * Chat / ModeBar / WelcomePane / ConfirmModal / ToolChain 是纯展示组件。
 */
import { useEffect, useRef, useState } from "react";
import { DownOutlined, MenuOutlined, SettingOutlined } from "@ant-design/icons";
import { Dropdown, Input, message, Tooltip } from "antd";
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
  updateSession,
  updateWorkspace,
} from "./api";
import type {
  AgentState,
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

const SESSION_KEY = "miniagent.session";
const MODE_KEY = "miniagent.mode";
const WEB_KEY = "miniagent.web";
const WS_KEY = "miniagent.workspace";

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
  const [collapsed, setCollapsed] = useState(false);
  // 顶栏会话标题：就地编辑状态
  const [titleEditing, setTitleEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  // 防止 Enter 提交后紧接着的 blur 二次提交 / 取消标志
  const titleDone = useRef(false);
  // 当前（或下一个新）对话使用的工作区；没选过就是默认工作区
  const [ws, setWs] = useState<string>(() => localStorage.getItem(WS_KEY) || "default");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
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
  const send = (text: string) => {
    const msg = text.trim();
    if (!msg || busy) return;
    const asstId = `a-${Date.now()}`;
    const userItem: ChatItem = {
      id: `u-${Date.now()}`,
      role: "user",
      content: msg,
      status: "done",
    };
    const asstItem: ChatItem = {
      id: asstId,
      role: "assistant",
      content: "",
      status: "streaming",
      mode,
    };
    setItems((prev) => [...prev, userItem, asstItem]);
    setBusy(true);
    const ac = new AbortController();
    abortRef.current = ac;

    chatStream(
      { message: msg, mode, web, session, workspace: ws },
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

  // 当前会话（可能还没落盘，所以找不到就是 undefined）
  const currentSession = sessions.find((s) => s.id === session);
  // 顶栏模型菜单里当前激活的那一项
  const activeModelId = models.find((m) => m.is_active)?.id;

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
    <div className="app">
      <aside className={`sidebar-fixed ${collapsed ? "collapsed" : ""}`}>
        <Sidebar
          workspaces={workspaces}
          sessions={sessions}
          currentId={session}
          collapsed={collapsed}
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
            <ModeBar mode={mode} web={web} state={state} onMode={setMode} onWeb={setWeb} />
          </div>
        </header>

        <Chat
          items={items}
          busy={busy}
          mode={mode}
          onSend={send}
          onStop={stop}
          focusId={focus?.id ?? null}
          workspaces={workspaces}
          workspaceId={ws}
          onWorkspaceChange={(id) => setWs(id)}
          workspaceLocked={!!currentSession && currentSession.message_count > 0}
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
          onTab={setSettingsTab}
          onClose={() => setSettingsOpen(false)}
          onChanged={() => {
            fetchState().then(setState).catch(() => {});
            refreshModels();
          }}
        />
      </div>
    </div>
  );
}
