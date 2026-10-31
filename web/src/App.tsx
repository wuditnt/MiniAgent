/**
 * 应用编排：模式/联网开关、消息流、SSE 消费、危险确认都在这里。
 * Chat / WelcomePane / ConfirmPanel / ToolChain 是纯展示组件。
 */
import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { DeleteOutlined, ExportOutlined, BarChartOutlined, LayoutOutlined, MenuOutlined, MoreOutlined, TeamOutlined, LockOutlined, LogoutOutlined, UserOutlined, GlobalOutlined } from "@ant-design/icons";
import { Dropdown, Input, message, Modal, Switch, Tooltip } from "antd";
import {
  chatStream,
  createWorkspace,
  deleteMessages,
  deleteSession,
  deleteWorkspace,
  fetchHistory,
  fetchModels,
  fetchRunning,
  fetchSessions,
  fetchState,
  fetchWorkspaces,
  pickFolder,
  postAnswer,
  postConfirm,
  postStop,
  reconnectStream,
  setSessionModel,
  truncateSession,
  updateSession,
  updateWorkspace,
} from "./api";
import type {
  AgentState,
  ApprovalMode,
  ChatItem,
  ConfirmInfo,
  HistoryMessage,
  Mode,
  ModelProvider,
  PendingAsk,
  ReasoningLevel,
  RunAction,
  RunInfo,
  SessionItem,
  SSEEvent,
  SkillItem,
  TraceEvent,
  User,
  WorkspaceItem,
} from "./types";
import Chat from "./components/Chat";
import { syncServerNow, serverNowMs } from "./serverClock";
import { skillsInSent, stripSkillMarks } from "./components/composer/skillMarks";
import Sidebar from "./components/Sidebar";
import WorkspaceModal from "./components/WorkspaceModal";
import WorkspaceMemoryModal from "./components/WorkspaceMemoryModal";
import SettingsModal from "./components/SettingsModal";
import type { SettingsTab } from "./components/SettingsModal";
import AccountSettingsModal from "./components/AccountSettingsModal";
import ChangePasswordModal from "./components/ChangePasswordModal";
import Workbench from "./components/Workbench";
import Trajectory from "./components/Trajectory";
import SkillManager from "./components/SkillManager";

const SESSION_KEY = "miniagent.session";
const MODE_KEY = "miniagent.mode";
const WS_KEY = "miniagent.workspace";
const APPROVAL_KEY = "miniagent.approval";
const SIDEBAR_W_KEY = "miniagent.sidebar-width";
const SIDEBAR_W_MIN = 220;
const SIDEBAR_W_MAX = 400;
const SIDEBAR_W_DEFAULT = 260;

/** 侧栏宽度：只接受 220～400 之间的值，非法/未设置时回到 260 */
function loadSidebarWidth(): number {
  const raw = Number(localStorage.getItem(SIDEBAR_W_KEY));
  if (!Number.isFinite(raw) || raw <= 0) return SIDEBAR_W_DEFAULT;
  return Math.min(SIDEBAR_W_MAX, Math.max(SIDEBAR_W_MIN, raw));
}

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

/** 历史消息 → 聊天气泡：历史回放与「接入后台任务」的快照共用同一口径 */
function toItems(messages: HistoryMessage[], runs?: RunInfo[]): ChatItem[] {
  // run 按「第几个用户轮次」挂到对应的助手气泡（一轮一条，后端保证顺序）
  const byTurn = new Map<number, RunInfo>();
  (runs || []).forEach((r) => byTurn.set(r.turn, r));
  let turn = 0;
  return messages.map((m, i) => {
    if (m.role === "user") turn += 1;
    return {
      id: `h-${i}`,
      role: m.role === "user" ? "user" : "assistant",
      content: m.content,
      status: "done" as const,
      // 落盘时一并保存的用量/耗时，回放时原样恢复脚注
      usage: m.usage,
      sec: m.sec,
      // 附件元数据：发送时写入，历史回放原样展示
      attachments: m.attachments,
      ts: m.ts,
      run: m.role === "user" ? undefined : byTurn.get(turn),
    };
  });
}

/** 工具参数压成一行摘要（运行时间线里显示「最后动作」用） */
function briefArgs(args?: Record<string, unknown>): string {
  if (!args) return "";
  const s = Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  return s.length > 80 ? `${s.slice(0, 80)}…` : s;
}

/** 往本轮时间线追加一条动作（没有 run 的历史气泡直接跳过） */
function pushAction(it: ChatItem, action: RunAction): ChatItem {
  if (!it.run) return it;
  return { ...it, run: { ...it.run, actions: [...it.run.actions, action] } };
}

/** 按 id 收尾一条工具动作（状态 + 耗时） */
function updateAction(it: ChatItem, id: string, patch: Partial<RunAction>): ChatItem {
  if (!it.run) return it;
  return {
    ...it,
    run: {
      ...it.run,
      actions: it.run.actions.map((a) => (a.id === id ? { ...a, ...patch } : a)),
    },
  };
}

/** 本轮终态：用时优先取服务端定稿值，缺失时用对齐后的服务端时间兜底 */
function endRun(
  it: ChatItem,
  status: RunInfo["status"],
  elapsedMs?: number | null
): ChatItem {
  if (!it.run) return it;
  const ms =
    elapsedMs ?? Math.max(0, Math.round(serverNowMs() - it.run.started_at * 1000));
  return { ...it, run: { ...it.run, status, elapsed_ms: ms } };
}

export default function App({
  user,
  loginEnabled = true,
  onLogout,
}: {
  user: User;
  loginEnabled?: boolean;
  onLogout: () => void;
}) {
  const [session, setSession] = useState<string>(loadSession);
  const [mode, setMode] = useState<Mode>(() =>
    localStorage.getItem(MODE_KEY) === "chat" ? "chat" : "agent"
  );
  const [web, setWeb] = useState<boolean>(false);
  const [approval, setApproval] = useState<ApprovalMode>(() => {
    const v = localStorage.getItem(APPROVAL_KEY);
    return v === "auto" || v === "full" ? v : "ask";
  });
  const [state, setState] = useState<AgentState | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [busy, setBusy] = useState(false);
  // 「编辑重发」的目标消息 id：点击编辑只进入编辑态、不删任何内容，
  // 真正替换旧轮次发生在用户再次发送时（见 send）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmInfo | null>(null);
  // 内联提问卡片：正在等用户作答的 ask_user 提问（回答/失效后折叠进助手气泡）
  const [ask, setAsk] = useState<PendingAsk | null>(null);
  const askRef = useRef<PendingAsk | null>(null);
  /** 当前正在流式的助手消息 id：问答卡片要折回它所在的这条气泡 */
  const curAsstRef = useRef<string | null>(null);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  // 模型单选菜单的数据源（/api/models）；模型 chip 已迁到输入框工具栏
  const [models, setModels] = useState<ModelProvider[]>([]);
  // 全局推理档位（low/high/max）
  const [reasoning, setReasoning] = useState<ReasoningLevel>("high");
  // provider id → 服务商展示名（来自 /api/models 的 templates），用于 chip 与菜单 pill
  const [providerNames, setProviderNames] = useState<Record<string, string>>({});
  const [wsModal, setWsModal] = useState<{
    mode: "create" | "edit";
    item?: WorkspaceItem;
    pickedPath?: string;
  } | null>(null);
  // 工作区记忆弹窗：从侧栏某个工作区的快捷工具打开
  const [wsMemory, setWsMemory] = useState<WorkspaceItem | null>(null);
  // 窄屏（≤768px）默认收起侧栏，给聊天区留足空间
  const [collapsed, setCollapsed] = useState(() => typeof window !== "undefined" && window.innerWidth <= 768);
  // 桌面端侧栏宽度：可拖拽，夹在 220～400 之间，localStorage 记住
  const [sidebarW, setSidebarW] = useState(loadSidebarWidth);
  // 拖拽过程直接写 CSS 变量，避免每帧重渲染整棵组件树；松手后再同步 state
  const appRef = useRef<HTMLDivElement>(null);
  const liveW = useRef(sidebarW);
  // 窄屏下侧栏是浮层（抽屉）：展开时点空白处应收起，所以需要知道当前是否浮层模式
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.innerWidth <= 768
  );

  // 屏幕宽度变化时自动收起/展开侧栏
  useEffect(() => {
    const onResize = () => {
      const narrow = window.innerWidth <= 768;
      setIsMobile(narrow);
      if (narrow && !collapsed) setCollapsed(true);
      else if (!narrow && collapsed) setCollapsed(false);
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
  // 新会话占位（草稿）：点「新会话」先在前端占一行，真正聊天前不落盘；
  // null = 无草稿；字符串 = 草稿归属的工作区 id（草稿会话 id 即当前 session）
  const [draftWs, setDraftWs] = useState<string | null>(null);
  // 草稿会话的模型选择（还没落盘，先暂存在前端；随第一条消息一起持久化）
  const [draftModel, setDraftModel] = useState<{
    modelId: string;
    reasoning: ReasoningLevel;
  } | null>(null);
  // 厂家 → 支持的推理档位（来自内置模板声明）
  const [tplLevels, setTplLevels] = useState<Record<string, ReasoningLevel[]>>({});
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [wbFullscreen, setWbFullscreen] = useState(false);
  // 技能管理页：占满主区（对话保持挂载，输入内容不丢）
  const [skillsPage, setSkillsPage] = useState(false);
  // 从技能管理页点「在对话框中试用」的一次性请求（nonce 保证只消费一次）
  const [trialSkill, setTrialSkill] = useState<{
    name: string;
    source: "builtin" | "custom";
    nonce: number;
  } | null>(null);
  // 运行轨迹抽屉（P0 事实层）：与工作台抽屉互斥，同一位置只开一个
  const [trajectoryOpen, setTrajectoryOpen] = useState(false);
  const [tjSession, setTjSession] = useState("");
  // 当前 run 的实时镜像事件（trace 帧），打开抽屉时并入时间线
  const [traces, setTraces] = useState<TraceEvent[]>([]);
  // 每次运行结束自增，通知 Trajectory 做一次磁盘全量兜底拉取
  const [tjReload, setTjReload] = useState(0);
  const [statsOpen, setStatsOpen] = useState(false);
  // 账号管理（仅超管）/ 修改自己的密码
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [pwdOpen, setPwdOpen] = useState(false);
  // 搜索命中后要定位闪烁的消息 id
  const [focus, setFocus] = useState<{ id: string; nonce: number } | null>(null);
  const pendingHit = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 正在执行的会话 id 集合（侧栏「运行中」指示器数据源）；
  // 发消息时本地乐观加一个，之后以 /api/running 轮询结果为准
  const [runningIds, setRunningIds] = useState<Set<string>>(() => new Set());
  /** 正在「接入」后台任务的流（与发起新消息的 abortRef 相互独立） */
  const attachedRef = useRef<AbortController | null>(null);
  /** 刚在本页收到终态的会话：避免轮询还没清掉 running 时又自动接入一遍 */
  const finishedSidRef = useRef<string | null>(null);

  // 开关状态持久化（逐消息生效：send 时读取当前 state，不依赖这里的时序）
  useEffect(() => {
    localStorage.setItem(MODE_KEY, mode);
  }, [mode]);
  useEffect(() => {
    localStorage.setItem(WS_KEY, ws);
  }, [ws]);
  useEffect(() => {
    localStorage.setItem(APPROVAL_KEY, approval);
  }, [approval]);

  /** 会话列表刷新（支持搜索词，跨全部工作区） */
  const refreshSessions = (q: string = "") => {
    fetchSessions(q)
      .then((list) => {
        setSessions(list);
        // 非搜索态才同步运行状态：搜索结果只是子集，不能拿它覆盖全局指示器
        if (!q) {
          setRunningIds(new Set(list.filter((s) => s.running).map((s) => s.id)));
        }
      })
      .catch(() => {});
  };

  /** 工作区列表刷新（含每个工作区下的会话数） */
  const refreshWorkspaces = () => {
    fetchWorkspaces().then(setWorkspaces).catch(() => {});
  };

  /** 模型列表刷新（输入框工具栏的模型 chip 菜单） */
  const refreshModels = () => {
    fetchModels()
      .then((m) => {
        setModels(m.providers);
        setReasoning(m.reasoning);
        setProviderNames(
          Object.fromEntries(Object.entries(m.templates).map(([k, t]) => [k, t.name]))
        );
        setTplLevels(
          Object.fromEntries(
            Object.entries(m.templates).map(([k, t]) => [
              k,
              t.reasoning?.levels ?? ["low", "high", "max"],
            ])
          ) as Record<string, ReasoningLevel[]>
        );
      })
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

  // 侧栏「运行中」指示器：3s 轮询后端内存状态（只读、不碰磁盘）。
  // 页面切到后台时暂停轮询，回到前台立即补拉一次 —— 用户看不到时没必要请求。
  useEffect(() => {
    let timer: number | undefined;
    let fails = 0;
    let stopped = false;
    const tick = () => {
      if (document.visibilityState !== "visible") return;
      fetchRunning()
        .then((map) => {
          if (stopped) return;
          fails = 0;
          setRunningIds(new Set(Object.keys(map)));
        })
        .catch(() => {
          if (stopped) return;
          // 单次失败保留旧值（网络抖动不该让指示器闪一下），连续两次才清空
          fails += 1;
          if (fails >= 2) setRunningIds(new Set());
        });
    };
    const loop = () => {
      tick();
      timer = window.setTimeout(loop, 3000);
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") tick();
    };
    loop();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // 切换会话时重新加载历史
  useEffect(() => {
    localStorage.setItem(SESSION_KEY, session);
    // 换会话就断开旧的「接入」流：后台任务不受影响，只是这边不看了
    attachedRef.current?.abort();
    attachedRef.current = null;
    setItems([]);
    setEditingId(null);   // 切换会话时退出编辑态，避免把编辑目标带到别的会话
    fetchHistory(session)
      .then((h) => {
        syncServerNow(h.server_now);
        setItems(toItems(h.messages, h.runs));
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

  // 切换会话时同步联网开关：每个对话保存自己的智能搜索配置，
  // 草稿会话（还没落盘）不在列表里，沿用当前选择
  useEffect(() => {
    if (!sessions.some((s) => s.id === session)) return;
    const cur = sessions.find((s) => s.id === session);
    setWeb(!!cur?.web);
  }, [session, sessions]);

  // 草稿一旦落盘（出现在会话列表里），撤掉占位，交给真实会话行
  useEffect(() => {
    if (draftWs !== null && sessions.some((s) => s.id === session)) {
      setDraftWs(null);
      setDraftModel(null); // 模型配置也由会话记录接管
    }
  }, [draftWs, session, sessions]);

  const patchAssistant = (id: string, patch: (it: ChatItem) => ChatItem) =>
    setItems((prev) => prev.map((it) => (it.id === id ? patch(it) : it)));

  /** 终态兜底：把还没等到 tool_end 的 pending 工具节点标记为失败 */
  const finalizeTools = (it: ChatItem): ChatItem => ({
    ...it,
    tools: (it.tools || []).map((t) =>
      t.ok === undefined ? { ...t, ok: false, result: "[cancelled] 任务未完成" } : t
    ),
  });

  /** 把待答卡片折叠进它所在的助手气泡；无答案（停止/断连）标记为失效 */
  const foldAsk = (asstId: string, answer?: string) => {
    const cur = askRef.current;
    if (!cur) return;
    askRef.current = null;
    setAsk(null);
    patchAssistant(asstId, (it) => ({
      ...it,
      ask: {
        question: cur.question,
        options: cur.options,
        allowFreeText: cur.allowFreeText,
        multi: cur.multi,
        answer,
        expired: !answer,
      },
    }));
  };

  /**
   * 接入一条正在后台执行的任务：先收快照重建对话，再实时消费本轮事件。
   * 场景：刷新页面 / 换标签页 / 断线回来 —— 执行一直在后端跑，这里只是重新「看」。
   */
  const attachRun = (sid: string) => {
    attachedRef.current?.abort();
    const ac = new AbortController();
    attachedRef.current = ac;
    const asstId = `r-${Date.now()}`;
    setBusy(true);
    setRunningIds((prev) => new Set(prev).add(sid));
    reconnectStream(
      sid,
      (ev) => {
        switch (ev.type) {
          case "snapshot": {
            syncServerNow(ev.server_now);
            const base = toItems(ev.messages, ev.runs);
            if (ev.running) {
              // 本轮还没结束：快照之后再挂一条流式气泡接收后续帧（带上已有时间线）
              setItems([
                ...base,
                {
                  id: asstId,
                  role: "assistant",
                  content: "",
                  status: "streaming",
                  mode,
                  ts: Date.now() / 1000,
                  run: ev.run || undefined,
                },
              ]);
              curAsstRef.current = asstId;
            } else {
              setItems(base);
            }
            break;
          }
          case "delta":
            patchAssistant(asstId, (it) =>
              ev.kind === "reasoning"
                ? { ...it, reasoning: (it.reasoning || "") + ev.text }
                : { ...it, content: it.content + ev.text }
            );
            break;
          case "run_start":
            syncServerNow(ev.server_now);
            patchAssistant(asstId, (it) => ({
              ...it,
              run: {
                turn: ev.turn,
                started_at: ev.started_at,
                ended_at: null,
                elapsed_ms: null,
                status: "running",
                actions: ev.actions,
              },
            }));
            break;
          case "tool_start":
            patchAssistant(asstId, (it) =>
              pushAction(
                { ...it, tools: [...(it.tools || []), { id: ev.id, name: ev.name, args: ev.args }] },
                {
                  ts: ev.ts ?? 0,
                  kind: "tool",
                  id: ev.id,
                  name: ev.name,
                  detail: briefArgs(ev.args),
                  status: "running",
                  dur_ms: null,
                }
              )
            );
            break;
          case "tool_end":
            patchAssistant(asstId, (it) =>
              updateAction(
                {
                  ...it,
                  tools: (it.tools || []).map((t) =>
                    t.id === ev.id ? { ...t, ok: ev.ok, sec: ev.sec, result: ev.result } : t
                  ),
                },
                ev.id,
                { status: ev.ok ? "ok" : "fail", dur_ms: ev.sec * 1000 }
              )
            );
            break;
          case "confirm_request":
            setConfirm({ id: ev.id, tool: ev.tool, args: ev.args });
            break;
          case "ask_request":
            askRef.current = {
              id: ev.id,
              question: ev.question,
              options: ev.options,
              allowFreeText: ev.allow_free_text,
              multi: ev.multi,
            };
            setAsk(askRef.current);
            break;
          case "notice":
            // 降级/重试提示进本轮时间线，不再单独占一条对话气泡
            patchAssistant(asstId, (it) =>
              pushAction(it, {
                ts: ev.ts ?? 0,
                kind: "notice",
                name: "降级提示",
                detail: ev.text,
                status: "info",
                dur_ms: null,
              })
            );
            break;
          case "done":
            // 与本页主动发送的 done 同口径：必须 endRun 定稿 run 状态，
            // 否则「接入」视角的时间线会永远停在「执行中」走表（另一浏览器发起的回合）
            patchAssistant(asstId, (it) =>
              endRun(
                {
                  ...it,
                  status: "done",
                  content: ev.answer || it.content,
                  sec: ev.sec,
                  usage: ev.usage,
                },
                "done",
                ev.elapsed_ms
              )
            );
            foldAsk(asstId);
            finishedSidRef.current = sid;
            if (ev.title_pending) window.setTimeout(() => refreshSessions(), 5000);
            break;
          case "error":
            patchAssistant(asstId, (it) =>
              endRun(
                finalizeTools({ ...it, status: "error", error: ev.message, usage: ev.usage }),
                "error",
                ev.elapsed_ms
              )
            );
            foldAsk(asstId);
            setConfirm(null);
            finishedSidRef.current = sid;
            break;
          case "cancelled":
            patchAssistant(asstId, (it) =>
              finalizeTools({ ...it, status: "cancelled", reason: ev.reason, usage: ev.usage })
            );
            foldAsk(asstId);
            setConfirm(null);
            finishedSidRef.current = sid;
            break;
          default:
            break;
        }
      },
      ac.signal
    )
      .catch((err: unknown) => {
        if (ac.signal.aborted) return;
        message.warning(err instanceof Error ? err.message : "接入运行失败");
      })
      .finally(() => {
        if (attachedRef.current === ac) attachedRef.current = null;
        setBusy(false);
        refreshAll();
        setTjReload((n) => n + 1);
      });
  };

  // 打开一个正在后台跑的会话（刷新 / 换标签页 / 断线回来）→ 自动接入实时输出
  useEffect(() => {
    if (busy || attachedRef.current) return;
    // 轮询确认已空闲：清掉「本页刚收过终态」标记，之后该会话再有后台任务才允许接入；
    // 必须放在 finishedSid 挡板之前，否则标记永远清不掉，后续真任务也接不上
    if (!runningIds.has(session)) {
      if (finishedSidRef.current === session) finishedSidRef.current = null;
      return;
    }
    // 本页刚收到终态、轮询还没把 running 清掉：不要把刚跑完的这轮再接入一遍
    if (finishedSidRef.current === session) return;
    attachRun(session);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, runningIds, busy]);

  /** 用户在内联卡片上作答：回传服务端并折叠卡片 */
  const handleAnswerAsk = (answer: string) => {
    const cur = askRef.current;
    if (!cur) return;
    postAnswer(cur.id, answer, session).catch(() => {});
    const asstId = curAsstRef.current;
    if (asstId) {
      foldAsk(asstId, answer);
    } else {
      askRef.current = null;
      setAsk(null);
    }
  };

  /** 权限审批的应答：回填给后端后立刻清掉本地待答状态（面板随之收起） */
  const handleAnswerConfirm = (approve: boolean) => {
    if (!confirm) return;
    postConfirm(confirm.id, approve, session).catch(() => {});
    setConfirm(null);
  };

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
    curAsstRef.current = asstId;
    const sentAt = Date.now() / 1000;
    // 技能在消息里用不可见字符包裹：气泡只显示名字
    const display = stripSkillMarks(msg).trim();
    const userItem: ChatItem = {
      id: `u-${Date.now()}`,
      role: "user",
      content: display || skillsInSent(msg).join(" "),
      status: "done",
      attachments,
      ts: sentAt,
    };
    const asstItem: ChatItem = {
      id: asstId,
      role: "assistant",
      content: "",
      status: "streaming",
      mode,
      ts: sentAt,
      // 起点先用本地时间占位，收到 run_start 后换成服务端的 started_at
      run: {
        turn: 0,
        started_at: Date.now() / 1000,
        ended_at: null,
        elapsed_ms: null,
        status: "running",
        actions: [
          {
            ts: 0,
            kind: "step",
            name: "开始本轮任务",
            detail: "读取会话上下文，注入工具表",
            status: "ok",
            dur_ms: null,
          },
        ],
      },
    };
    if (skipUserItem) {
      // 重新生成场景：user 消息已保留在列表里，只需追加 assistant 气泡
      setItems((prev) => [...prev, asstItem]);
    } else {
      setItems((prev) => [...prev, userItem, asstItem]);
    }
    setBusy(true);
    // 乐观点亮侧栏指示器：不必等下一轮轮询，发出去这一刻就显示「运行中」
    setRunningIds((prev) => new Set(prev).add(session));
    setTraces([]); // 新一轮 run：清掉上一轮的实时镜像
    const ac = new AbortController();
    abortRef.current = ac;
    // 409 分支把收尾交给「接入」流，避免 finally 又把 busy 打回空闲
    let handedToAttach = false;

    chatStream(
      {
        message: msg, mode, web, approval, session, workspace: ws, attachments,
        // 会话级模型配置：新会话（草稿）在首条消息时随请求落盘
        model_id: effModelId,
        reasoning: effReasoning,
      },
      (ev: SSEEvent) => {
        switch (ev.type) {
          case "delta":
            patchAssistant(asstId, (it) =>
              ev.kind === "reasoning"
                ? { ...it, reasoning: (it.reasoning || "") + ev.text }
                : { ...it, content: it.content + ev.text }
            );
            break;
          case "run_start":
            syncServerNow(ev.server_now);
            patchAssistant(asstId, (it) => ({
              ...it,
              run: {
                turn: ev.turn,
                started_at: ev.started_at,
                ended_at: null,
                elapsed_ms: null,
                status: "running",
                actions: ev.actions,
              },
            }));
            break;
          case "tool_start":
            patchAssistant(asstId, (it) =>
              pushAction(
                { ...it, tools: [...(it.tools || []), { id: ev.id, name: ev.name, args: ev.args }] },
                {
                  ts: ev.ts ?? 0,
                  kind: "tool",
                  id: ev.id,
                  name: ev.name,
                  detail: briefArgs(ev.args),
                  status: "running",
                  dur_ms: null,
                }
              )
            );
            break;
          case "tool_end":
            patchAssistant(asstId, (it) =>
              updateAction(
                {
                  ...it,
                  tools: (it.tools || []).map((t) =>
                    t.id === ev.id ? { ...t, ok: ev.ok, sec: ev.sec, result: ev.result } : t
                  ),
                },
                ev.id,
                { status: ev.ok ? "ok" : "fail", dur_ms: ev.sec * 1000 }
              )
            );
            break;
          case "confirm_request":
            setConfirm({ id: ev.id, tool: ev.tool, args: ev.args });
            break;
          case "ask_request":
            // 内联提问：卡片渲染在当前流式的助手气泡里，等用户点选/输入
            askRef.current = {
              id: ev.id,
              question: ev.question,
              options: ev.options,
              allowFreeText: ev.allow_free_text,
              multi: ev.multi,
            };
            setAsk(askRef.current);
            break;
          case "notice":
            // 降级/重试提示进本轮时间线，不再单独占一条对话气泡
            patchAssistant(asstId, (it) =>
              pushAction(it, {
                ts: ev.ts ?? 0,
                kind: "notice",
                name: "降级提示",
                detail: ev.text,
                status: "info",
                dur_ms: null,
              })
            );
            break;
          case "memory":
            // 用户显式「请帮我记住 XXX」：写入成功/重复/被开关拦截都给一条轻提示
            if (ev.ok) {
              message.success(ev.duplicate ? "该内容已在用户画像中" : "已记入用户画像");
            } else {
              message.warning(ev.error || "未记入用户画像");
            }
            break;
          case "done":
            // answer 是服务端合并后的最终回答，比逐帧拼接更可靠
            patchAssistant(asstId, (it) =>
              endRun(
                {
                  ...it,
                  status: "done",
                  content: ev.answer || it.content,
                  sec: ev.sec,
                  usage: ev.usage,
                },
                "done",
                ev.elapsed_ms
              )
            );
            foldAsk(asstId); // 若有未答的卡片，随本轮结束标记失效
            // 本页刚收到终态：挡住自动接入 effect 在轮询还没清掉 running 时
            // 把这轮再 attach 一遍（snapshot 会整份重建 items，滚动锚点随之失效）
            finishedSidRef.current = session;
            // 标题没被用户命名过：记忆提炼会在后台再补一个模型标题，但它晚于
            // done 才落盘（那时这条 SSE 连接已经关了），所以稍后自己再拉一次列表
            if (ev.title_pending) window.setTimeout(() => refreshSessions(), 5000);
            break;
          case "error":
            patchAssistant(asstId, (it) =>
              endRun(
                finalizeTools({ ...it, status: "error", error: ev.message, usage: ev.usage }),
                "error",
                ev.elapsed_ms
              )
            );
            foldAsk(asstId);
            setConfirm(null);
            finishedSidRef.current = session;
            break;
          case "cancelled":
            // 串行路径取消时 agent 不发 tool_end：这里把挂着的 pending 节点收尾，避免永远转圈
            patchAssistant(asstId, (it) =>
              endRun(
                finalizeTools({ ...it, status: "cancelled", reason: ev.reason, usage: ev.usage }),
                "cancelled",
                ev.elapsed_ms
              )
            );
            foldAsk(asstId);
            setConfirm(null);
            finishedSidRef.current = session;
            break;
          case "trace":
            // 事件流的实时镜像（可选）：累积给轨迹抽屉并排展示，落盘全量仍以兜底拉取为准
            setTraces((prev) => (prev.length >= 4000 ? prev : [...prev, ev.event]));
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
        if (/已有任务在执行/.test(msg)) {
          // 这个会话其实还在后台跑（多半是另一个标签页发起的）：
          // 撤回乐观插入的气泡，改为接入那条运行的实时输出
          setItems((prev) =>
            prev.filter((it) => it.id !== asstId && it.id !== userItem.id)
          );
          handedToAttach = true;
          finishedSidRef.current = null;
          message.info("该对话正在后台执行，已接入实时输出");
          attachRun(session);
          return;
        }
        patchAssistant(asstId, (it) => ({ ...it, status: "error", error: msg }));
        foldAsk(asstId);
      })
      .finally(() => {
        if (handedToAttach) return;
        setBusy(false);
        refreshAll(); // 对话结束后刷新列表（标题/时间/计数可能变了）
        setTjReload((n) => n + 1); // 运行结束：让轨迹抽屉拉一次磁盘全量兜底
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

  /** 编辑重发：让最后一条 user 气泡原位变成内联编辑框（取消即恢复原样） */
  const handleEditResend = (msgId: string) => {
    const idx = items.findIndex((it) => it.id === msgId);
    if (idx < 0 || items[idx].role !== "user") return;
    setEditingId(msgId);
  };

  /** 内联编辑框的「发送」：先截掉该条 user 及其后的本轮内容，再用新文本重发 */
  const sendEdit = (text: string) => {
    if (busy) return;
    const id = editingId;
    setEditingId(null);
    if (!id) return;
    const idx = items.findIndex((it) => it.id === id);
    if (idx < 0 || items[idx].role !== "user") return;
    const msg = text.trim();
    if (!msg) return;
    // keep = 该 user 消息之前所有非 notice 消息（不含这条 user 本身）
    const keep = items.slice(0, idx).filter((it) => it.role !== "notice").length;
    truncateSession(session, keep)
      .then(() => {
        setItems((prev) => prev.slice(0, idx));
        send(msg);
      })
      .catch((e: unknown) => message.error(e instanceof Error ? e.message : "编辑重发失败"));
  };

  /** 开一个未落盘的草稿会话（前端先占位），真正聊天时才由后端落盘 */
  const startDraft = () => {
    const id =
      typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setDraftWs(ws);
    // 新对话的智能搜索默认关闭（每个对话各自独立配置）
    setWeb(false);
    setSession(id);
  };

  /** 新建会话：按指定模式（对话 / 任务）；已有草稿则只切换模式，不重复占位 */
  const handleNewSession = (nextMode: Mode = mode) => {
    setMode(nextMode);
    if (draftWs !== null) return;
    startDraft();
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
    setDraftWs(null); // 离开草稿占位：新会话还没聊天就被切走 → 占位消失
    setDraftModel(null);
    // 还原该会话保存的模式（老会话没有该字段时保持当前模式）
    const target = sessions.find((s) => s.id === sid);
    if (target?.mode) setMode(target.mode);
    pendingHit.current = hasHit ? (hitIndex as number) : null;
    setSession(sid);
  };

  /** 打开某会话的运行轨迹抽屉（与工作台互斥，同一位置只开一个） */
  const openTrajectory = (sid: string) => {
    setWorkbenchOpen(false);
    setWbFullscreen(false);
    setTjSession(sid);
    setTrajectoryOpen(true);
  };

  /** 侧栏会话项「回放」：先切到该会话，再打开它的轨迹 */
  const handleReplaySession = (sid: string) => {
    if (sid !== session) handleSelectSession(sid);
    openTrajectory(sid);
  };

  // 轨迹抽屉开着时切换会话 → 轨迹跟随当前会话
  useEffect(() => {
    if (trajectoryOpen && session && session !== tjSession) setTjSession(session);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, trajectoryOpen]);

  /** 删除对话：删当前则自动切到列表里的下一个 */
  const handleDeleteSession = (sid: string) => {
    deleteSession(sid)
      .then(() => {
        if (sid === session) {
          const rest = sessions.filter((s) => s.id !== sid);
          // 还有别的会话 → 切过去；删完了 → 改在前端占一个草稿位（不落盘）
          if (rest.length) setSession(rest[0].id);
          else startDraft();
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

  /** 多选删除：ids → 展示项序号（不含 notice）交后端任意删；本地先过滤、失败回滚 */
  const handleDeleteMessages = (ids: string[]) => {
    const idset = new Set(ids);
    if (!idset.size) return;
    const indexes: number[] = [];
    let ord = 0;
    for (const it of items) {
      if (it.role === "notice") continue;
      if (idset.has(it.id)) indexes.push(ord);
      ord += 1;
    }
    if (!indexes.length) return;
    const snapshot = items;
    setItems((prev) => prev.filter((it) => !idset.has(it.id)));
    deleteMessages(session, indexes).catch((e: unknown) => {
      message.error(e instanceof Error ? e.message : "删除失败");
      setItems(snapshot); // 失败回滚：恢复到删除前
    });
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
  // 模型 chip 菜单里当前激活的那一项（全局默认，新会话的缺省模型）
  const activeModelId = models.find((m) => m.is_active)?.id;
  // ---- 会话级模型配置（每个对话单独保存，互不影响）----
  const isDraft = draftWs !== null && !currentSession;
  // 会话保存值 > 草稿暂存值 > 全局默认
  const effModelId = currentSession?.model_id || draftModel?.modelId || activeModelId || "";
  const effReasoning: ReasoningLevel =
    currentSession?.reasoning || draftModel?.reasoning || reasoning;
  const effModelName = models.find((m) => m.id === effModelId)?.name || "";
  // 当前模型的厂家支持的推理档位（未知厂家按通用三档）
  const effProvider = models.find((m) => m.id === effModelId)?.provider || "";
  const reasoningLevels: ReasoningLevel[] =
    tplLevels[effProvider] || ["low", "high", "max"];

  /** 切换当前会话的模型：持久化到会话，别的对话不受影响 */
  const handleActivateModel = (id: string) => {
    if (isDraft) {
      // 草稿还没落盘：先暂存在前端，随第一条消息一起保存
      setDraftModel((p) => ({ modelId: id, reasoning: p?.reasoning ?? effReasoning }));
      return;
    }
    setSessionModel(session, { model_id: id })
      .then(() => refreshSessions())
      .catch((e: unknown) =>
        message.error(e instanceof Error ? e.message : "切换模型失败")
      );
  };

  /** 设置当前会话的推理等级（只在智能体模式下生效） */
  const handleSetReasoning = (level: ReasoningLevel) => {
    if (isDraft) {
      setDraftModel((p) => ({ modelId: p?.modelId ?? effModelId, reasoning: level }));
      return;
    }
    setSessionModel(session, { reasoning: level })
      .then(() => refreshSessions())
      .catch((e: unknown) =>
        message.error(e instanceof Error ? e.message : "设置推理等级失败")
      );
  };

  /** 切换当前会话的智能搜索：持久化到会话，别的对话不受影响 */
  const handleSetWeb = (w: boolean) => {
    setWeb(w);
    if (!currentSession) return; // 会话还没落盘（草稿）：随首条消息一起保存
    setSessionModel(session, { web: w })
      .then(() => refreshSessions())
      .catch((e: unknown) => {
        setWeb(!w);
        message.error(e instanceof Error ? e.message : "保存联网配置失败");
      });
  };

  /** 打开「模型」设置页 */
  const handleManageModels = () => {
    setSettingsTab("model");
    setSettingsOpen(true);
  };
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

  /** 侧栏右边缘拖拽：实时写 --sidebar-w，松手后再同步 state 与 localStorage */
  const startSidebarResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (isMobile || collapsed) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = sidebarW;
    document.body.classList.add("sidebar-resizing");

    const move = (ev: PointerEvent) => {
      const w = Math.min(SIDEBAR_W_MAX, Math.max(SIDEBAR_W_MIN, startW + ev.clientX - startX));
      liveW.current = w;
      appRef.current?.style.setProperty("--sidebar-w", `${w}px`);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      document.body.classList.remove("sidebar-resizing");
      setSidebarW(liveW.current);
      localStorage.setItem(SIDEBAR_W_KEY, String(liveW.current));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  };

  /** 双击拖拽条：恢复默认宽度 */
  const resetSidebarWidth = () => {
    liveW.current = SIDEBAR_W_DEFAULT;
    setSidebarW(SIDEBAR_W_DEFAULT);
    localStorage.setItem(SIDEBAR_W_KEY, String(SIDEBAR_W_DEFAULT));
  };

  return (
    <div
      ref={appRef}
      className={`app ${wbFullscreen ? "wb-fullscreen" : ""}`}
      style={{
        "--sidebar-w": collapsed ? "0px" : `${isMobile ? SIDEBAR_W_DEFAULT : sidebarW}px`,
      } as CSSProperties}
    >
      {/* 窄屏抽屉遮罩：点击空白处收起侧栏（桌面端侧栏常驻，不显示） */}
      {!collapsed && isMobile && (
        <div className="sidebar-backdrop" onClick={() => setCollapsed(true)} />
      )}
      <aside className={`sidebar-fixed ${collapsed ? "collapsed" : ""}`}>
        {/* 桌面端拖拽条：拖动改宽、双击复位；窄屏抽屉不启用 */}
        {!collapsed && !isMobile && (
          <div
            className="sidebar-resizer"
            title="拖动调整宽度，双击恢复默认"
            onPointerDown={startSidebarResize}
            onDoubleClick={resetSidebarWidth}
          />
        )}
        <Sidebar
          workspaces={workspaces}
          sessions={sessions}
          draft={
            draftWs !== null && !sessions.some((s) => s.id === session)
              ? { id: session, workspaceId: draftWs, mode }
              : null
          }
          currentId={session}
          collapsed={collapsed}
          remote={!!state?.remote}
          runningIds={runningIds}
          onToggleCollapse={() => setCollapsed((c) => !c)}
          onSelect={handleSelectSession}
          onCreateSession={handleNewSession}
          onDeleteSession={handleDeleteSession}
          onCreateWorkspace={handleCreateWorkspace}
          onRenameWorkspace={handleRenameWorkspace}
          onDeleteWorkspace={handleDeleteWorkspace}
          onOpenWorkspaceMemory={setWsMemory}
          onSearch={(q) => refreshSessions(q)}
          onRenameSession={handleRenameSession}
          onReplaySession={handleReplaySession}
          onOpenSettings={() => {
            setSettingsTab("general");
            setSettingsOpen(true);
          }}
          onOpenSkills={() => {
            setSkillsPage(true);
            setWorkbenchOpen(false);
            setTrajectoryOpen(false);
          }}
        />
      </aside>

      <div className={`main ${skillsPage ? "skill-view" : ""}`}>
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
            {/* 运行状态条：本对话正在执行（含后台执行、刚接入的那一刻） */}
            {runningIds.has(session) && (
              <span className="header-running" title="该对话正在执行，关掉页面也会继续跑完">
                <span className="header-running-dot" />
                {busy ? "执行中" : "后台执行中"}
              </span>
            )}
          </div>

          <div className="header-right">
            {/* 会话快捷操作：用量统计 / 清空会话 / 导出 Markdown 收进一个菜单 */}
            <Tooltip title="更多操作">
              <Dropdown
                trigger={["click"]}
                placement="bottomRight"
                menu={{
                  items: [
                    {
                      key: "web",
                      label: (
                        <div
                          className="menu-switch-row"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleSetWeb(!web);
                          }}
                        >
                          <span className="menu-switch-label">
                            <GlobalOutlined />
                            <span>智能搜索</span>
                          </span>
                          <Switch
                            size="small"
                            checked={web}
                            onClick={(_, e) => e.stopPropagation()}
                            onChange={(v) => handleSetWeb(v)}
                          />
                        </div>
                      ),
                    },
                    { type: "divider" as const },
                    {
                      key: "stats",
                      icon: <BarChartOutlined />,
                      label: "用量统计",
                    },
                    {
                      key: "export",
                      icon: <ExportOutlined />,
                      label: "导出为 Markdown",
                    },
                    { type: "divider" as const },
                    {
                      key: "clear",
                      icon: <DeleteOutlined />,
                      label: "清空会话",
                      danger: true,
                    },
                  ],
                  onClick: ({ key }) => {
                    if (key === "stats") setStatsOpen(true);
                    else if (key === "export") handleExportMarkdown();
                    else if (key === "clear") handleClearSession();
                  },
                }}
              >
                <span className="tb-icon-btn">
                  <MoreOutlined />
                </span>
              </Dropdown>
            </Tooltip>
            {/* 联网搜索开关已迁到输入框工具栏（每个对话独立配置） */}
            {/* 账号菜单：账号相关功能统一收在这个用户图标下（--no-login 时没有身份，不显示） */}
            {loginEnabled && (
              <Tooltip title={user.display_name || user.username}>
                <Dropdown
                  trigger={["click"]}
                  placement="bottomRight"
                  menu={{
                    items: [
                      {
                        key: "me",
                        disabled: true,
                        label: (
                          <span className="user-menu-me">
                            <span className="user-menu-name">
                              {user.display_name || user.username}
                            </span>
                            <span className="user-menu-role">
                              {user.role === "superadmin" ? "超级管理员" : "普通用户"}
                            </span>
                          </span>
                        ),
                      },
                      { type: "divider" as const },
                      ...(user.role === "superadmin"
                        ? [
                            {
                              key: "accounts",
                              icon: <TeamOutlined />,
                              label: "账号管理",
                            },
                          ]
                        : []),
                      {
                        key: "password",
                        icon: <LockOutlined />,
                        label: "修改密码",
                      },
                      { type: "divider" as const },
                      {
                        key: "logout",
                        icon: <LogoutOutlined />,
                        label: "退出登录",
                      },
                    ],
                    onClick: ({ key }) => {
                      if (key === "accounts") setAccountsOpen(true);
                      else if (key === "password") setPwdOpen(true);
                      else if (key === "logout") onLogout();
                    },
                  }}
                >
                  <span className="tb-icon-btn user-btn">
                    <UserOutlined />
                  </span>
                </Dropdown>
              </Tooltip>
            )}
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
                    setTrajectoryOpen(false); // 与轨迹抽屉互斥
                  }
                }}
              >
                <LayoutOutlined />
              </span>
            </Tooltip>
          </div>
        </header>

        <Chat
          sessionId={session}
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
          onWorkspaceChange={(id) => {
            setWs(id);
            // 草稿还没落盘：占位跟着用户选的工作区走
            if (draftWs !== null) setDraftWs(id);
          }}
          workspaceLocked={!!currentSession && currentSession.message_count > 0}
          onRegenerate={busy ? undefined : handleRegenerate}
          onEditResend={busy ? undefined : handleEditResend}
          editingId={editingId}
          onEditSend={sendEdit}
          onCancelEdit={() => setEditingId(null)}
          onDeleteMessages={busy ? undefined : handleDeleteMessages}
          onTrajectory={() => openTrajectory(session)}
          contextUsage={contextUsage}
          contextLimit={contextLimit}
          pendingAsk={ask}
          onAnswerAsk={handleAnswerAsk}
          confirm={confirm}
          onAnswerConfirm={handleAnswerConfirm}
          models={models}
          providerNames={providerNames}
          activeModelId={effModelId || undefined}
          modelName={effModelName}
          reasoning={effReasoning}
          reasoningLevels={reasoningLevels}
          onActivateModel={handleActivateModel}
          onSetReasoning={handleSetReasoning}
          onManageModels={handleManageModels}
          trialSkill={trialSkill}
        />

        {skillsPage && (
          <SkillManager
            workspaceId={ws}
            onClose={() => setSkillsPage(false)}
            onTrial={(skill: SkillItem) => {
              setSkillsPage(false);
              setMode("agent");     // 试用技能必须处于智能体模式
              setTrialSkill({ name: skill.name, source: skill.source, nonce: Date.now() });
            }}
          />
        )}

        <WorkspaceModal
          open={wsModal !== null}
          mode={wsModal?.mode || "create"}
          initial={wsModal?.item || null}
          pickedPath={wsModal?.pickedPath ?? null}
          onCancel={() => setWsModal(null)}
          onSubmit={handleWorkspaceSubmit}
        />

        {/* 工作区记忆：从左侧抽屉的工作区快捷工具打开 */}
        <WorkspaceMemoryModal
          open={wsMemory !== null}
          workspace={wsMemory}
          onClose={() => setWsMemory(null)}
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

        {loginEnabled && (
          <>
            <AccountSettingsModal
              open={accountsOpen}
              current={user}
              onClose={() => setAccountsOpen(false)}
            />
            <ChangePasswordModal open={pwdOpen} onClose={() => setPwdOpen(false)} />
          </>
        )}

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

      <Trajectory
        open={trajectoryOpen}
        session={tjSession || session}
        liveEvents={tjSession === session ? traces : []}
        reloadNonce={tjReload}
        onClose={() => setTrajectoryOpen(false)}
      />
    </div>
  );
}
