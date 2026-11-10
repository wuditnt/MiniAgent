/**
 * 应用编排：模式/联网开关、消息流、SSE 消费、危险确认都在这里。
 * Chat / WelcomePane / ConfirmPanel / ToolChain 是纯展示组件。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { DeleteOutlined, ExportOutlined, BarChartOutlined, LayoutOutlined, MenuOutlined, MoreOutlined, TeamOutlined, LockOutlined, LogoutOutlined, UserOutlined, BgColorsOutlined, CheckOutlined } from "@ant-design/icons";
import { Dropdown, Input, message, Modal, Tooltip } from "antd";
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
  markSessionRead,
  pickFolder,
  postAnswer,
  postConfirm,
  postStop,
  setSessionModel,
  truncateSession,
  updateSession,
  updateWorkspace,
} from "./api";
import type {
  AgentState,
  ApprovalMode,
  Artifact,
  ChatItem,
  HistoryMessage,
  Mode,
  ModelProvider,
  ReasoningLevel,
  RunAction,
  RunInfo,
  SessionItem,
  SSEEvent,
  SkillItem,
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
import { MAX_BACKGROUND, useSessionRuntime } from "./useSessionRuntime";
import { closeSessionStream, setInterest, setStreamHandler } from "./sessionStream";
import { useTheme, type ThemeMode } from "./theme";
import Trajectory from "./components/Trajectory";
import SkillManager from "./components/SkillManager";
import ConnectorsPage from "./components/ConnectorsPage";
import AutomationsPage from "./components/AutomationsPage";

const SESSION_KEY = "miniagent.session";
const MODE_KEY = "miniagent.mode";
const WS_KEY = "miniagent.workspace";

/** 主区视图：左栏导航与之一一对应；新增页面只需在这里加一项 */
type MainView = "chat" | "skills" | "connectors" | "automations";
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
      // 本轮工具产出的图片：刷新 / 重启后照样能在气泡下方看到
      artifacts: m.artifacts,
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
  // 会话保活：每个会话一份运行时，切走运行中的会话不销毁，切回直接渲染
  const rtApi = useSessionRuntime(session);
  const rt = rtApi.ensure(session);
  const { items, confirm, ask, traces } = rt;
  // 本页正在发送（POST 流）的会话：期间不把它放进共享流兴趣集，避免同一帧收两遍
  const [posting, setPosting] = useState<string | null>(null);
  // 本页订阅了实时事件的会话（当前会话优先 + 最多 MAX_BACKGROUND 个后台运行会话）
  const [subscribed, setSubscribed] = useState<Set<string>>(() => new Set());
  // 「正在看」的会话有实时来源（共享流订阅或本页发送）才算 busy
  const busy = rt.streaming || posting === session;
  /** 当前会话的 items 写入器（沿用原来的 setItems 调用口径） */
  const setItems = (
    next: ChatItem[] | ((prev: ChatItem[]) => ChatItem[])
  ) =>
    rtApi.update(session, (r) => {
      r.items = typeof next === "function" ? next(r.items) : next;
      r.loaded = true;
    });
  /** 按消息 id 打补丁：后台会话的流也写到自己那份运行时里 */
  const patchAssistant = (
    sid: string,
    id: string,
    patch: (it: ChatItem) => ChatItem
  ) =>
    rtApi.update(sid, (r) => {
      r.items = r.items.map((it) => (it.id === id ? patch(it) : it));
      r.loaded = true;
    });
  // 后台会话正在等确认/回答：侧栏红点 + 顶部横幅的提示数据源
  const attentionRts = rtApi.attention;
  const attentionIds = new Set(attentionRts.map((r) => r.sid));
  // 「编辑重发」的目标消息 id：点击编辑只进入编辑态、不删任何内容，
  // 真正替换旧轮次发生在用户再次发送时（见 send）；切会话时重置。
  const [editingId, setEditingId] = useState<string | null>(null);
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
  // 草稿会话勾选的连接器（null = 跟全局默认；首条消息时随请求落盘）
  const [draftConnectors, setDraftConnectors] = useState<string[] | null>(null);
  // 厂家 → 支持的推理档位（来自内置模板声明）
  const [tplLevels, setTplLevels] = useState<Record<string, ReasoningLevel[]>>({});
  const [settingsOpen, setSettingsOpen] = useState(false);
  // 主题：用户菜单的快捷入口与系统配置里的选项共用同一份状态
  const { mode: themeMode, setMode: setThemeMode } = useTheme();
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [wbFullscreen, setWbFullscreen] = useState(false);
  // 主区视图：chat = 对话（含右侧工作台），其余为占满整块主区的功能页
  const [view, setView] = useState<MainView>("chat");
  // 从技能管理页点「在对话框中试用」的一次性请求（nonce 保证只消费一次）
  const [trialSkill, setTrialSkill] = useState<{
    name: string;
    source: "builtin" | "custom";
    nonce: number;
  } | null>(null);
  // 运行轨迹抽屉（P0 事实层）：与工作台抽屉互斥，同一位置只开一个
  const [trajectoryOpen, setTrajectoryOpen] = useState(false);
  const [tjSession, setTjSession] = useState("");
  // 从某条消息打开轨迹时要定位的轮次（null = 普通打开，全部保持折叠）
  const [tjFocusTurn, setTjFocusTurn] = useState<number | null>(null);
  // 每次点击「查看运行轨迹」都自增：同一轮重复点击也能强制重新展开并滚动
  const [tjFocusNonce, setTjFocusNonce] = useState(0);
  // 每次运行结束自增，通知 Trajectory 做一次磁盘全量兜底拉取
  const [tjReload, setTjReload] = useState(0);
  const [statsOpen, setStatsOpen] = useState(false);
  // 账号管理（仅超管）/ 修改自己的密码
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [pwdOpen, setPwdOpen] = useState(false);
  // 搜索命中后要定位闪烁的消息 id
  const [focus, setFocus] = useState<{ id: string; nonce: number } | null>(null);
  const pendingHit = useRef<number | null>(null);
  // 正在执行的会话 id 集合（侧栏「运行中」指示器数据源）；
  // 发消息时本地乐观加一个，之后以 /api/running 轮询结果为准
  const [runningIds, setRunningIds] = useState<Set<string>>(() => new Set());
  // 运行状态由「空闲 → 运行」时自增：驱动当前会话重新拉一次历史（拿到本轮用户消息与草稿）
  const [runReload, setRunReload] = useState(0);
  const prevRunningRef = useRef<Set<string>>(new Set());
  /** 上一个会话 id：切走时保存滚动/草稿并决定是否释放运行时 */
  const prevSidRef = useRef(session);

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
          const ids = new Set(list.filter((s) => s.running).map((s) => s.id));
          setRunningIds(ids);
          rtApiRef.current.clearFinished(ids);
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

  /**
   * 立刻同步一次「运行中会话」：自动化任务开始后调用，避免等 3s 轮询。
   * 运行集合变化会驱动订阅集合更新，进而收到带本轮消息与草稿的 snapshot。
   */
  const syncRunning = useCallback(() => {
    if (document.visibilityState !== "visible") return Promise.resolve();
    return fetchRunning()
      .then((map) => {
        const ids = new Set(Object.keys(map));
        setRunningIds(ids);
        rtApiRef.current.clearFinished(ids);
      })
      .catch(() => {});
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
          const ids = new Set(Object.keys(map));
          setRunningIds(ids);
          // 轮询确认已空闲：清掉「本页刚收过终态」挡板，下一轮任务才允许重新接入
          rtApiRef.current.clearFinished(ids);
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

  // 当前会话从「空闲」变「运行」：补拉一次历史，拿到本轮用户消息与已生成草稿
  useEffect(() => {
    const was = prevRunningRef.current.has(session);
    prevRunningRef.current = runningIds;
    if (runningIds.has(session) && !was) setRunReload((n) => n + 1);
  }, [runningIds, session]);

  // 切换会话：运行中的会话保活（SSE 不断），非运行的释放数据、只留滚动与草稿
  useEffect(() => {
    localStorage.setItem(SESSION_KEY, session);
    const prev = prevSidRef.current;
    if (prev !== session) {
      prevSidRef.current = session;
      setEditingId(null);   // 切换会话时退出编辑态，避免把编辑目标带到别的会话
      const leaving = rtApi.ensure(prev);
      if (!leaving.streaming) rtApi.release(prev);
    }
    rtApi.prune(session);
    const cur = rtApi.ensure(session);
    // 流已建立：交给实时帧，不重复拉历史
    if (cur.streaming) {
      const hit = pendingHit.current;
      pendingHit.current = null;
      if (hit != null && hit >= 0 && hit < cur.items.length) jumpToHit(hit);
      return;
    }
    // 已有缓存且当前空闲：直接渲染；运行中的会话必须重拉，才能看到本轮消息与草稿
    if (cur.loaded && !runningIds.has(session)) {
      const hit = pendingHit.current;
      pendingHit.current = null;
      if (hit != null && hit >= 0 && hit < cur.items.length) jumpToHit(hit);
      return;
    }
    fetchHistory(session)
      .then((h) => {
        // 切走后又切回来了才有必要写入；流已抢先建立时交给 snapshot
        if (prevSidRef.current !== session || rtApi.isStreaming(session)) return;
        syncServerNow(h.server_now);
        // 若本次是从搜索结果点进来的，定位到命中那条消息并闪烁
        const hit = pendingHit.current;
        pendingHit.current = null;
        const base = toItems(h.messages, h.runs);
        if (h.running && h.run) {
          // 后台还在跑这一轮：先用 history 里的草稿把已生成内容补出来，
          // 再接入 SSE 续播增量 —— 不必等 3s 一次的 /api/running 轮询
          const asstId = `r-${Date.now()}`;
          const run: RunInfo = h.run;
          rtApi.update(session, (r) => {
            r.items = [
              ...base,
              {
                id: asstId,
                role: "assistant",
                content: h.live?.content || "",
                reasoning: h.live?.reasoning || undefined,
                status: "streaming",
                mode,
                ts: Date.now() / 1000,
                run,
              },
            ];
            r.loaded = true;
            r.curAsstId = asstId;
          }, true);
          // 交给共享流接手：放回运行集合，订阅集合会带上它并拉一份快照
          setRunningIds((prev) => new Set(prev).add(session));
        } else {
          rtApi.update(session, (r) => {
            r.items = base;
            r.loaded = true;
            r.curAsstId = null;
          }, true);
        }
        if (hit != null && hit >= 0 && hit < h.messages.length) {
          jumpToHit(hit);
        }
      })
      .catch(() => {});
    // runReload：运行开始信号，触发重拉；不监听 runningIds 本身，避免每次轮询都重拉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, runReload]);

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

  // 切换会话时同步联网开关：每个对话保存自己的联网搜索配置，
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

  /** 追加工具产出的图片：同一路径只保留一次（多个工具可能重复报告同一个文件） */
  const addArtifacts = (it: ChatItem, items: Artifact[]): ChatItem => {
    const known = new Set((it.artifacts || []).map((a) => a.path));
    const fresh = items.filter((a) => !known.has(a.path));
    return fresh.length ? { ...it, artifacts: [...(it.artifacts || []), ...fresh] } : it;
  };

  /** 终态兜底：把还没等到 tool_end 的 pending 工具节点标记为失败 */
  const finalizeTools = (it: ChatItem): ChatItem => ({
    ...it,
    tools: (it.tools || []).map((t) =>
      t.ok === undefined ? { ...t, ok: false, result: "[cancelled] 任务未完成" } : t
    ),
  });

  /** 把待答卡片折叠进它所在的助手气泡；无答案（停止/断连）标记为失效 */
  const foldAsk = (sid: string, asstId: string, answer?: string) => {
    const cur = rtApi.ensure(sid).ask;
    if (!cur) return;
    rtApi.update(sid, (r) => {
      r.ask = null;
    }, true);
    patchAssistant(sid, asstId, (it) => ({
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

  /** 为一轮还没有气泡的实时事件补一个流式助手气泡（快照没赶上时的兜底） */
  const asstFor = (sid: string): string => {
    const cur = rtApi.ensure(sid).curAsstId;
    if (cur) return cur;
    const id = `r-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`;
    rtApi.update(sid, (r) => {
      r.curAsstId = id;
      r.loaded = true;
      r.items = [
        ...r.items,
        { id, role: "assistant", content: "", status: "streaming", mode, ts: Date.now() / 1000 },
      ];
    }, true);
    return id;
  };

  /** 本轮终态：挡住轮询未清时的重复接入，并让侧栏尽快摘掉「运行中」 */
  const settleRun = (sid: string) => {
    rtApi.update(sid, (r) => {
      r.finished = true;
    });
    setRunningIds((prev) => {
      if (!prev.has(sid)) return prev;
      const next = new Set(prev);
      next.delete(sid);
      return next;
    });
    refreshAll();
    setTjReload((n) => n + 1);
  };

  /**
   * 实时帧的统一处理：后台共享流与本页 POST 流共用同一套逻辑。
   * 返回 true 表示收到了本轮终态。
   */
  const handleFrame = (sid: string, ev: SSEEvent): boolean => {
    switch (ev.type) {
      case "snapshot": {
        syncServerNow(ev.server_now);
        const base = toItems(ev.messages, ev.runs);
        const asstId = `r-${Date.now()}`;
        rtApi.update(sid, (r) => {
          r.items = ev.running
            ? [
                ...base,
                {
                  id: asstId,
                  role: "assistant",
                  // 本轮还没结束：先补上已生成但未定稿的正文/思考，再接收后续帧
                  content: ev.live?.content || "",
                  reasoning: ev.live?.reasoning || undefined,
                  status: "streaming",
                  mode,
                  ts: Date.now() / 1000,
                  run: ev.run || undefined,
                },
              ]
            : base;
          r.loaded = true;
          r.curAsstId = ev.running ? asstId : null;
          if (!ev.running) r.finished = true;
        }, true);
        return false;
      }
      case "delta": {
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) =>
          ev.kind === "reasoning"
            ? { ...it, reasoning: (it.reasoning || "") + ev.text }
            : { ...it, content: it.content + ev.text }
        );
        return false;
      }
      case "run_start": {
        syncServerNow(ev.server_now);
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) => ({
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
        return false;
      }
      case "tool_start": {
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) =>
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
        return false;
      }
      case "tool_end": {
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) =>
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
        return false;
      }
      case "artifact": {
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) => addArtifacts(it, ev.items));
        return false;
      }
      case "confirm_request":
        rtApi.update(sid, (r) => {
          r.confirm = { id: ev.id, tool: ev.tool, args: ev.args };
        }, true);
        return false;
      case "ask_request":
        // 内联提问：卡片渲染在当前流式的助手气泡里，等用户点选/输入
        rtApi.update(sid, (r) => {
          r.ask = {
            id: ev.id,
            question: ev.question,
            options: ev.options,
            allowFreeText: ev.allow_free_text,
            multi: ev.multi,
          };
        }, true);
        return false;
      case "notice": {
        const asstId = asstFor(sid);
        // 降级/重试提示进本轮时间线，不再单独占一条对话气泡
        patchAssistant(sid, asstId, (it) =>
          pushAction(it, {
            ts: ev.ts ?? 0,
            kind: "notice",
            name: "降级提示",
            detail: ev.text,
            status: "info",
            dur_ms: null,
          })
        );
        return false;
      }
      case "memory":
        // 用户显式「请帮我记住 XXX」：写入成功/重复/被开关拦截都给一条轻提示
        if (ev.ok) message.success(ev.duplicate ? "该内容已在用户画像中" : "已记入用户画像");
        else message.warning(ev.error || "未记入用户画像");
        return false;
      case "done": {
        const asstId = asstFor(sid);
        // answer 是服务端合并后的最终回答，比逐帧拼接更可靠
        patchAssistant(sid, asstId, (it) =>
          endRun(
            { ...it, status: "done", content: ev.answer || it.content, sec: ev.sec, usage: ev.usage },
            "done",
            ev.elapsed_ms
          )
        );
        foldAsk(sid, asstId); // 若有未答的卡片，随本轮结束标记失效
        settleRun(sid);
        // 标题没被用户命名过：记忆提炼会在后台再补一个模型标题，晚于 done 落盘
        if (ev.title_pending) window.setTimeout(() => refreshSessions(), 5000);
        return true;
      }
      case "error": {
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) =>
          endRun(
            finalizeTools({ ...it, status: "error", error: ev.message, usage: ev.usage }),
            "error",
            ev.elapsed_ms
          )
        );
        foldAsk(sid, asstId);
        rtApi.update(sid, (r) => {
          r.confirm = null;
        }, true);
        settleRun(sid);
        return true;
      }
      case "cancelled": {
        // 串行路径取消时 agent 不发 tool_end：这里把挂着的 pending 节点收尾，避免永远转圈
        const asstId = asstFor(sid);
        patchAssistant(sid, asstId, (it) =>
          endRun(
            finalizeTools({ ...it, status: "cancelled", reason: ev.reason, usage: ev.usage }),
            "cancelled",
            ev.elapsed_ms
          )
        );
        foldAsk(sid, asstId);
        rtApi.update(sid, (r) => {
          r.confirm = null;
        }, true);
        settleRun(sid);
        return true;
      }
      case "trace":
        // 事件流的实时镜像（可选）：累积给轨迹抽屉并排展示，落盘全量仍以兜底拉取为准
        rtApi.update(sid, (r) => {
          if (r.traces.length < 4000) r.traces = [...r.traces, ev.event];
        });
        return false;
      default:
        return false; // turn / llm_done：前端无需单独处理
    }
  };

  // 共享流只有一个处理器入口；用 ref 保证每次重渲染后闭包都是最新的
  const frameRef = useRef<(ev: SSEEvent) => void>(() => {});
  frameRef.current = (ev) => {
    if (!ev.session) return;
    // 已释放的会话（LRU/切走清理）收到残留帧也不重建，免得凭空长出一条消息
    if (!rtApiRef.current.has(ev.session)) return;
    handleFrame(ev.session, ev);
  };
  const rtApiRef = useRef(rtApi);
  rtApiRef.current = rtApi;

  useEffect(() => {
    setStreamHandler((ev) => frameRef.current(ev));
    return () => closeSessionStream();
  }, []);

  // 订阅集合：当前会话优先；其余后台运行会话按「已在保活」优先，最多 MAX_BACKGROUND 个
  useEffect(() => {
    setSubscribed((prev) => {
      const curRunning = runningIds.has(session) && !rtApiRef.current.isFinished(session);
      const cap = MAX_BACKGROUND + (curRunning ? 1 : 0);
      const next = new Set<string>();
      if (curRunning) next.add(session);
      const others = [...runningIds]
        .filter((sid) => sid !== session && !rtApiRef.current.isFinished(sid))
        .sort((a, b) => Number(prev.has(b)) - Number(prev.has(a)));
      for (const sid of others) {
        if (next.size >= cap) break;
        next.add(sid);
      }
      if (next.size === prev.size && [...next].every((sid) => prev.has(sid))) return prev;
      return next;
    });
  }, [runningIds, session]);

  // 兴趣集 = 订阅集合 - 本页正在发送的会话（POST 自己就是一条流，避免同一帧收两遍）
  const interest = useMemo(() => {
    const set = new Set(subscribed);
    if (posting) set.delete(posting);
    return [...set].sort();
  }, [subscribed, posting]);
  const interestKey = interest.join(",");
  useEffect(() => {
    setInterest(interest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interestKey]);

  // 运行时上的 streaming 标记 = 「本页有实时来源」（共享流订阅或正在 POST）
  useEffect(() => {
    const set = new Set(subscribed);
    if (posting) set.add(posting);
    for (const sid of set) rtApi.ensure(sid);
    rtApi.syncStreaming(set);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscribed, posting]);

  /** 用户在内联卡片上作答：回传服务端并折叠卡片 */
  const handleAnswerAsk = (answer: string) => {
    const cur = ask;
    if (!cur) return;
    postAnswer(cur.id, answer, session).catch(() => {});
    const asstId = rt.curAsstId;
    if (asstId) {
      foldAsk(session, asstId, answer);
    } else {
      rtApi.update(session, (r) => { r.ask = null; }, true);
    }
  };

  /** 权限审批的应答：回填给后端后立刻清掉本地待答状态（面板随之收起） */
  const handleAnswerConfirm = (approve: boolean) => {
    if (!confirm) return;
    postConfirm(confirm.id, approve, session).catch(() => {});
    rtApi.update(session, (r) => { r.confirm = null; }, true);
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
    const sid = session;
    const asstId = `a-${Date.now()}`;
    rtApi.update(sid, (r) => {
      r.curAsstId = asstId;
    });
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
    setRunningIds((prev) => new Set(prev).add(session));
    rtApi.update(sid, (r) => {
      r.traces = []; // 新一轮 run：清掉上一轮的实时镜像
    });
    const ac = new AbortController();
    // 本页发送期间：该会话由这条 POST 流负责，不进共享流兴趣集；但 streaming 标记
    // 要立刻打开，否则切走会话时会被当成空闲运行时释放掉
    rtApi.setStreaming(sid, true);
    setPosting(sid);
    // 409 分支把收尾交给共享流，避免 finally 又把 busy 打回空闲
    let handedToAttach = false;
    // 是否收到本轮终态：没收到就断流时不自动重连（同共享流的 finished 挡板）
    let gotTerminal = false;

    chatStream(
      {
        message: msg, mode, web, approval, session, workspace: ws, attachments,
        // 会话级模型配置：新会话（草稿）在首条消息时随请求落盘
        model_id: effModelId,
        reasoning: effReasoning,
        // 会话级连接器勾选：草稿先暂存，随首条消息一起落盘
        connectors: isDraft ? draftConnectors : (currentSession?.connectors ?? null),
      },
      (ev: SSEEvent) => {
        switch (ev.type) {
          case "delta":
            patchAssistant(sid, asstId, (it) =>
              ev.kind === "reasoning"
                ? { ...it, reasoning: (it.reasoning || "") + ev.text }
                : { ...it, content: it.content + ev.text }
            );
            break;
          case "run_start":
            syncServerNow(ev.server_now);
            patchAssistant(sid, asstId, (it) => ({
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
            patchAssistant(sid, asstId, (it) =>
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
            patchAssistant(sid, asstId, (it) =>
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
          case "artifact":
            // 工具把图片写进了工作区：气泡下方直接给缩略图，不用等模型自己引用
            patchAssistant(sid, asstId, (it) => addArtifacts(it, ev.items));
            break;
          case "confirm_request":
            rtApi.update(sid, (r) => {
              r.confirm = { id: ev.id, tool: ev.tool, args: ev.args };
            }, true);
            break;
          case "ask_request":
            // 内联提问：卡片渲染在当前流式的助手气泡里，等用户点选/输入
            // 内联提问：卡片渲染在当前流式的助手气泡里，等用户点选/输入
            rtApi.update(sid, (r) => {
              r.ask = {
                id: ev.id,
                question: ev.question,
                options: ev.options,
                allowFreeText: ev.allow_free_text,
                multi: ev.multi,
              };
            }, true);
            break;
          case "notice":
            // 降级/重试提示进本轮时间线，不再单独占一条对话气泡
            patchAssistant(sid, asstId, (it) =>
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
            gotTerminal = true;
            patchAssistant(sid, asstId, (it) =>
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
            foldAsk(sid, asstId); // 若有未答的卡片，随本轮结束标记失效
            // 本页刚收到终态：挡住自动接入 effect 在轮询还没清掉 running 时
            // 把这轮再 attach 一遍（snapshot 会整份重建 items，滚动锚点随之失效）
            rtApi.update(sid, (r) => {
              r.finished = true;
            });
            // 标题没被用户命名过：记忆提炼会在后台再补一个模型标题，但它晚于
            // done 才落盘（那时这条 SSE 连接已经关了），所以稍后自己再拉一次列表
            if (ev.title_pending) window.setTimeout(() => refreshSessions(), 5000);
            break;
          case "error":
            gotTerminal = true;
            patchAssistant(sid, asstId, (it) =>
              endRun(
                finalizeTools({ ...it, status: "error", error: ev.message, usage: ev.usage }),
                "error",
                ev.elapsed_ms
              )
            );
            foldAsk(sid, asstId);
            rtApi.update(sid, (r) => {
              r.confirm = null;
              r.finished = true;
            }, true);
            break;
          case "cancelled":
            // 串行路径取消时 agent 不发 tool_end：这里把挂着的 pending 节点收尾，避免永远转圈
            gotTerminal = true;
            patchAssistant(sid, asstId, (it) =>
              endRun(
                finalizeTools({ ...it, status: "cancelled", reason: ev.reason, usage: ev.usage }),
                "cancelled",
                ev.elapsed_ms
              )
            );
            foldAsk(sid, asstId);
            rtApi.update(sid, (r) => {
              r.confirm = null;
              r.finished = true;
            }, true);
            break;
          case "trace":
            // 事件流的实时镜像（可选）：累积给轨迹抽屉并排展示，落盘全量仍以兜底拉取为准
            rtApi.update(sid, (r) => {
              if (r.traces.length < 4000) r.traces = [...r.traces, ev.event];
            });
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
          rtApi.update(sid, (r) => {
            r.items = r.items.filter(
              (it) => it.id !== asstId && it.id !== userItem.id
            );
          }, true);
          handedToAttach = true;
          rtApi.update(sid, (r) => {
            r.finished = false;
          });
          message.info("该对话正在后台执行，已接入实时输出");
          // 交给共享流接管：把它放回运行集合，订阅集合会带上它并拉快照
          setRunningIds((prev) => new Set(prev).add(sid));
          return;
        }
        patchAssistant(sid, asstId, (it) => ({ ...it, status: "error", error: msg }));
        foldAsk(sid, asstId);
      })
      .finally(() => {
        // POST 流结束：交还共享流（若该会话仍在运行，订阅集合会把它接回去）
        setPosting(null);
        if (handedToAttach) return;
        if (!gotTerminal && !ac.signal.aborted) {
          // 流意外中断：先挡住重复接入，等 /api/running 清零后再允许接入下一轮
          rtApi.update(sid, (r) => {
            r.finished = true;
          });
        }
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
    // 新对话的联网搜索默认关闭（每个对话各自独立配置）
    setWeb(false);
    // 连接器勾选回到「跟全局默认」
    setDraftConnectors(null);
    setSession(id);
  };

  /** 新建会话：按指定模式（对话 / 任务）；已有草稿则只切换模式，不重复占位 */
  const handleNewSession = (nextMode: Mode = mode) => {
    setView("chat");
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
    setView("chat"); // 会话属于导航：在任何功能页点会话都回到对话
    const hasHit = typeof hitIndex === "number" && hitIndex >= 0;
    const target = sessions.find((s) => s.id === sid);
    // 自动化会话：进入即清未读（红点只提醒「有新结果」，看过就消失）
    if (target?.unread) {
      markSessionRead(sid).catch(() => {});
      setSessions((list) => list.map((s) => (s.id === sid ? { ...s, unread: false } : s)));
    }
    if (sid === session) {
      // 已停在该会话：历史就在屏上，直接定位即可
      if (hasHit) jumpToHit(hitIndex as number);
      return;
    }
    setDraftWs(null); // 离开草稿占位：新会话还没聊天就被切走 → 占位消失
    setDraftModel(null);
    // 还原该会话保存的模式（老会话没有该字段时保持当前模式）
    if (target?.mode) setMode(target.mode);
    pendingHit.current = hasHit ? (hitIndex as number) : null;
    setSession(sid);
  };

  /** 打开某会话的运行轨迹抽屉（与工作台互斥，同一位置只开一个） */
  const openTrajectory = (sid: string, turn?: number) => {
    setWorkbenchOpen(false);
    setWbFullscreen(false);
    setTjSession(sid);
    setTjFocusTurn(turn ?? null);
    setTjFocusNonce((n) => n + 1);
    setTrajectoryOpen(true);
  };

  /** 侧栏会话项「回放」：先切到该会话，再打开它的轨迹 */
  const handleReplaySession = (sid: string) => {
    setView("chat");
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
  // 显示名称为空时回退到模型 ID（与模型选择菜单保持一致）
  const effModel = models.find((m) => m.id === effModelId);
  const effModelName = effModel?.name || effModel?.model || "";
  // 当前模型的厂家支持的推理档位（未知厂家按通用三档）
  const effProvider = effModel?.provider || "";
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

  /** 切换当前会话的联网搜索：持久化到会话，别的对话不受影响 */
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

  /** 当前会话的连接器勾选：草稿只暂存前端，已有会话直接改本地列表（保存由菜单内完成） */
  const handleConnectorsChange = (list: string[] | null) => {
    if (isDraft) {
      setDraftConnectors(list);
      return;
    }
    setSessions((prev) =>
      prev.map((s) => (s.id === session ? { ...s, connectors: list } : s))
    );
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

  /** 底部人员菜单：头像 + 名字，账号功能统一收在这里（--no-login 时没有身份，不渲染） */
  const accountMenu = loginEnabled ? (
    <Tooltip
      title={`${user.display_name || user.username} · ${
        user.role === "superadmin" ? "超级管理员" : "普通用户"
      }`}
      placement="top"
    >
      <Dropdown
        trigger={["click"]}
        placement="topLeft"
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
            {
              key: "theme",
              icon: <BgColorsOutlined />,
              label: "主题",
              children: [
                { key: "theme-system", label: "跟随系统" },
                { key: "theme-light", label: "浅色" },
                { key: "theme-dark", label: "深色" },
              ].map((it) => ({
                ...it,
                icon:
                  themeMode === it.key.replace("theme-", "") ? (
                    <CheckOutlined />
                  ) : (
                    <span style={{ display: "inline-block", width: 14 }} />
                  ),
              })),
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
            else if (key.startsWith("theme-")) {
              setThemeMode(key.slice("theme-".length) as ThemeMode);
            }
          },
        }}
      >
        <span className="sidebar-user">
          {/* 默认头像：渐变圆底 + 白色人形；后续支持自定义时在这里换成 <img> 即可 */}
          <span className="user-avatar">
            <UserOutlined />
          </span>
          <span className="sidebar-user-name">{user.display_name || user.username}</span>
        </span>
      </Dropdown>
    </Tooltip>
  ) : null;

  return (
    <div
      ref={appRef}
      className={`app ${wbFullscreen && view === "chat" ? "wb-fullscreen" : ""} ${
        view !== "chat" ? "page-view" : ""
      }`}
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
          version={state?.version}
          remote={!!state?.remote}
          runningIds={runningIds}
          attentionIds={attentionIds}
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
            setView("skills");
            setTrajectoryOpen(false);
          }}
          onOpenConnectors={() => {
            setView("connectors");
            setTrajectoryOpen(false);
          }}
          onOpenAutomations={() => {
            setView("automations");
            setTrajectoryOpen(false);
          }}
          automationsUnread={
            sessions.filter((s) => s.source === "automation" && s.unread).length
          }
          activeView={view}
          accountMenu={accountMenu}
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
            {/* 联网搜索开关已移到输入框上方，右对齐输入框 */}
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

        {/* 后台会话在等确认/回答：顶部横幅提示，点击切过去处理 */}
        {attentionRts.length > 0 && (
          <div
            className="session-attention-banner"
            onClick={() => handleSelectSession(attentionRts[0].sid)}
          >
            <span className="session-attention-dot" />
            <span className="session-attention-text">
              {attentionRts.length === 1
                ? `「${
                    sessions.find((s) => s.id === attentionRts[0].sid)?.title ||
                    "另一个对话"
                  }」正在等待你的确认/回答`
                : `${attentionRts.length} 个对话正在等待你的确认/回答`}
            </span>
            <span className="session-attention-action">去处理</span>
          </div>
        )}

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
          web={web}
          onWeb={handleSetWeb}
          onRegenerate={busy ? undefined : handleRegenerate}
          onEditResend={busy ? undefined : handleEditResend}
          editingId={editingId}
          onEditSend={sendEdit}
          onCancelEdit={() => setEditingId(null)}
          onDeleteMessages={busy ? undefined : handleDeleteMessages}
          onTrajectory={(turn?: number) => openTrajectory(session, turn)}
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
          connectors={isDraft ? draftConnectors : (currentSession?.connectors ?? null)}
          connectorsDraft={isDraft}
          onConnectorsChange={handleConnectorsChange}
          initialScrollTop={rtApi.scrollOf(session)}
          onScrollTop={(top) => rtApi.saveScroll(session, top)}
          draft={rtApi.draftOf(session)}
          onDraftChange={(text) => rtApi.saveDraft(session, text)}
        />

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

      {/* 功能页：占满整个主区（对话区 + 右侧工作台）；对话与工作台保持挂载，返回时状态不丢 */}
      {view !== "chat" && (
        <div className="page-shell">
          {view === "skills" && (
            <SkillManager
              workspaceId={ws}
              onTrial={(skill: SkillItem) => {
                setView("chat");
                setMode("agent");     // 试用技能必须处于智能体模式
                setTrialSkill({ name: skill.name, source: skill.source, nonce: Date.now() });
              }}
            />
          )}
          {view === "connectors" && (
            <ConnectorsPage remote={!!state?.remote} />
          )}
          {view === "automations" && (
            <AutomationsPage
              workspaces={workspaces}
              models={models}
              workspaceId={ws}
              remote={!!state?.remote}
              onSessionCreated={() => {
                refreshSessions();
                syncRunning();
              }}
              onOpenSession={(sid) => handleSelectSession(sid)}
            />
          )}
        </div>
      )}

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
        focusTurn={tjFocusTurn}
        focusNonce={tjFocusNonce}
        onClose={() => setTrajectoryOpen(false)}
      />
    </div>
  );
}
