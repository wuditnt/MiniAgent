/**
 * 后端 API 封装：普通 JSON 接口 + POST /api/chat 的 SSE 消费。
 *
 * EventSource 只支持 GET，所以用 fetch 读流并手写解析 SSE 帧（约 30 行），
 * 不引入 XStream —— 这也是方案里约定的降级路径，干脆作为唯一实现。
 */
import type {
  AgentState,
  ApprovalMode,
  Attachment,
  GeneralSettings,
  HistoryMessage,
  MemoryData,
  Mode,
  ModelsInfo,
  SessionItem,
  SettingsInfo,
  SkillItem,
  SSEEvent,
  TestResult,
  ToolInfo,
  TrajectoryData,
  User,
  UserRole,
  WorkspaceDirData,
  WorkspaceFileData,
  WorkspaceItem,
  WorkspaceTablePage,
} from "./types";

/** 从错误响应里尽量提取可读信息（后端错误统一为 {error: "..."}） */
async function readError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (data && typeof data.error === "string") return data.error;
  } catch {
    /* 非 JSON 响应体，退回状态码 */
  }
  return `HTTP ${res.status}`;
}

// ---------------------------------------------------------------- 请求出口

/** 401 时通知上层切回登录页（由 AuthGate 注册，避免每个调用方各写一遍） */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

/** 统一请求出口：仅额外处理 401，其余原样返回给调用方判断 */
async function req(input: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(input, init);
  if (res.status === 401) unauthorizedHandler?.();
  return res;
}

export async function fetchState(): Promise<AgentState> {
  const res = await req("/api/state");
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function fetchHistory(
  session: string
): Promise<{ session: string; messages: HistoryMessage[] }> {
  const res = await req(`/api/history?session=${encodeURIComponent(session)}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 只读回放会话事件流（P0 事实层）；老会话无事件文件时 events 为空数组 */
export async function fetchTrajectory(
  session: string,
  fromSeq = 0,
  limit = 500
): Promise<TrajectoryData> {
  const qs = new URLSearchParams({
    session,
    from_seq: String(fromSeq),
    limit: String(limit),
  });
  const res = await req(`/api/trajectory?${qs}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function postConfirm(
  id: string,
  approve: boolean,
  session: string
): Promise<void> {
  const res = await req("/api/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, approve, session }),
  });
  if (!res.ok) throw new Error(await readError(res));
}

/** 回答 Agent 的内联提问（ask_request）：answer 为空串表示未作答 */
export async function postAnswer(
  id: string,
  answer: string,
  session: string
): Promise<void> {
  const res = await req("/api/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, answer, session }),
  });
  if (!res.ok) throw new Error(await readError(res));
}

export async function postStop(session: string): Promise<void> {
  const res = await req("/api/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session }),
  });
  if (!res.ok) throw new Error(await readError(res));
}

/** 上传附件（base64）：先落盘到工作区 .attachments/，返回元数据供发送时引用 */
export async function uploadFile(
  file: File,
  workspace: string
): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const res = await req("/api/upload", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: file.name,
            data: (reader.result as string).split(",")[1] || "",
            mime: file.type || "application/octet-stream",
            workspace,
          }),
        });
        if (!res.ok) throw new Error(await readError(res));
        resolve(await res.json());
      } catch (e) {
        reject(e);
      }
    };
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

export async function fetchSessions(
  q: string = "",
  workspace: string = ""
): Promise<SessionItem[]> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (workspace) params.set("workspace", workspace);
  const qs = params.toString();
  const res = await req(`/api/sessions${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).sessions;
}

export async function createSession(
  workspace: string = ""
): Promise<{ id: string; title: string; workspace_id: string }> {
  const res = await req("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function deleteSession(sid: string): Promise<void> {
  const res = await req(`/api/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await readError(res));
}

/** 截断会话：保留前 keep 条 user/assistant 消息（不含 system），删除其后的全部内容 */
export async function truncateSession(sid: string, keep: number): Promise<{ ok: boolean; keep: number }> {
  const res = await req(`/api/sessions/${encodeURIComponent(sid)}/truncate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 修改会话标题（改过之后后端不再按首条消息自动生成） */
export async function updateSession(sid: string, title: string): Promise<{ id: string; title: string }> {
  const res = await req(`/api/sessions/${encodeURIComponent(sid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/**
 * 设置本会话自己的配置（持久化到会话 JSON，各对话互不影响）。
 * model_id / reasoning / web 传 undefined 表示「保持原值」。
 */
export async function setSessionModel(
  sid: string,
  opts: { model_id?: string; reasoning?: string; web?: boolean }
): Promise<{ ok: boolean; model_id: string | null; reasoning: string | null; web: boolean }> {
  const res = await req(`/api/sessions/${encodeURIComponent(sid)}/model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

// ---------------------------------------------------------------- 工作区

/** 弹出本机原生「选择文件夹」对话框（后端 tkinter 实现）；取消返回 null */
export async function pickFolder(): Promise<string | null> {
  const res = await req("/api/workspaces/pick", { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).path ?? null;
}

export async function fetchWorkspaces(): Promise<WorkspaceItem[]> {
  const res = await req("/api/workspaces");
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).workspaces;
}

// ---------------------------------------------------------- 工作台文件浏览

export async function fetchWorkspaceDir(
  workspaceId: string,
  path = ""
): Promise<WorkspaceDirData> {
  const qs = new URLSearchParams({ workspace_id: workspaceId, path });
  const res = await req(`/api/workspace/dir?${qs}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function fetchWorkspaceFile(
  workspaceId: string,
  path: string
): Promise<WorkspaceFileData> {
  const qs = new URLSearchParams({ workspace_id: workspaceId, path });
  const res = await req(`/api/workspace/file?${qs}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 原始字节预览地址（图片 / PDF 等二进制预览共用） */
export function workspaceRawUrl(workspaceId: string, path: string): string {
  const qs = new URLSearchParams({ workspace_id: workspaceId, path });
  return `/api/workspace/raw?${qs}`;
}

/** 文件下载地址：后端以 attachment 形式返回，浏览器直接落盘 */
export function workspaceDownloadUrl(workspaceId: string, path: string): string {
  const qs = new URLSearchParams({ workspace_id: workspaceId, path });
  return `/api/workspace/download?${qs}`;
}

/** CSV/TSV 表格分页：后端按行窗口 × 列窗口返回 */
export async function fetchWorkspaceTable(
  workspaceId: string,
  path: string,
  opts: { rowOffset?: number; rowLimit?: number; colOffset?: number; colLimit?: number } = {},
): Promise<WorkspaceTablePage> {
  const qs = new URLSearchParams({ workspace_id: workspaceId, path });
  if (opts.rowOffset !== undefined) qs.set("row_offset", String(opts.rowOffset));
  if (opts.rowLimit !== undefined) qs.set("row_limit", String(opts.rowLimit));
  if (opts.colOffset !== undefined) qs.set("col_offset", String(opts.colOffset));
  if (opts.colLimit !== undefined) qs.set("col_limit", String(opts.colLimit));
  const res = await req(`/api/workspace/table?${qs}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** GET /api/skills：可用技能清单（内置 / 自定义） */
export async function fetchSkills(): Promise<SkillItem[]> {
  const res = await req("/api/skills");
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).skills;
}

export async function createWorkspace(
  name: string,
  path: string
): Promise<WorkspaceItem> {
  const res = await req("/api/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, path }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function updateWorkspace(
  wid: string,
  patch: { name?: string; path?: string }
): Promise<WorkspaceItem> {
  const res = await req(`/api/workspaces/${encodeURIComponent(wid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function deleteWorkspace(wid: string): Promise<void> {
  const res = await req(`/api/workspaces/${encodeURIComponent(wid)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await readError(res));
}

// ---------------------------------------------------------------- 模型配置

export async function fetchModels(): Promise<ModelsInfo> {
  const res = await req("/api/models");
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function createModel(payload: {
  provider: string;
  name: string;
  base_url: string;
  api_key: string;
  model: string;
}): Promise<{ id: string }> {
  const res = await req("/api/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function updateModel(
  id: string,
  patch: { name?: string; base_url?: string; api_key?: string; model?: string }
): Promise<void> {
  const res = await req(`/api/models/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(await readError(res));
}

export async function deleteModel(id: string): Promise<void> {
  const res = await req(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await readError(res));
}

export async function testModel(id: string): Promise<TestResult> {
  const res = await req(`/api/models/${encodeURIComponent(id)}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function testModelRaw(base_url: string, api_key: string): Promise<TestResult> {
  const res = await req("/api/models/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_url, api_key }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 设置全局推理等级（off/low/high/max），返回生效值 */
export async function setModelReasoning(
  level: string
): Promise<{ ok: boolean; reasoning: string }> {
  const res = await req("/api/models/reasoning", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

// ---------------------------------------------------------------- 通用配置

export async function fetchSettings(): Promise<SettingsInfo> {
  const res = await req("/api/settings");
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 获取全部工具列表（插件管理界面用） */
export async function fetchTools(): Promise<{ tools: ToolInfo[]; groups: Record<string, ToolInfo[]>; sources: Record<string, number> }> {
  const res = await req("/api/tools");
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 更新通用配置；传 {reset: true} 表示恢复默认，其余字段做局部覆盖 */
export async function updateSettings(
  patch: Partial<GeneralSettings> | { reset: true }
): Promise<SettingsInfo> {
  const res = await req("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

// ---------------------------------------------------------------- 记忆

/** 读取当前工作区的三层记忆（用户画像 / 项目记忆 / 主题摘要） */
export async function fetchMemory(workspaceId: string): Promise<MemoryData> {
  const res = await req(`/api/memory?workspace_id=${encodeURIComponent(workspaceId)}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/** 保存手工编辑的记忆内容（user=用户画像，project=当前工作区项目记忆） */
export async function saveMemory(
  scope: "user" | "project",
  content: string,
  workspaceId: string
): Promise<void> {
  const res = await req("/api/memory", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, content, workspace_id: workspaceId }),
  });
  if (!res.ok) throw new Error(await readError(res));
}

/** 删除一条主题摘要 */
export async function deleteMemoryTopic(session: string, workspaceId: string): Promise<void> {
  const res = await req("/api/memory/topic", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session, workspace_id: workspaceId }),
  });
  if (!res.ok) throw new Error(await readError(res));
}

// ---------------------------------------------------------------- 登录与账号

/** 当前登录状态；未登录返回 user=null（不抛异常，供启动时判断） */
export async function fetchAuthMe(): Promise<{ user: User | null; login_enabled: boolean }> {
  const res = await req("/api/auth/me");
  if (res.status === 401) return { user: null, login_enabled: true };
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function login(username: string, password: string): Promise<User> {
  const res = await req("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).user;
}

export async function logout(): Promise<void> {
  const res = await req("/api/auth/logout", { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
}

/** 修改自己的密码（首次登录强制改密走同一个接口） */
export async function changePassword(
  oldPassword: string,
  newPassword: string
): Promise<User> {
  const res = await req("/api/auth/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).user;
}

// ---------------------------------------------------------------- 账号管理（超管）

export async function fetchUsers(): Promise<User[]> {
  const res = await req("/api/users");
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).users;
}

export async function createUser(payload: {
  username: string;
  password: string;
  display_name?: string;
  role?: UserRole;
  must_change_password?: boolean;
}): Promise<User> {
  const res = await req("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).user;
}

export async function updateUser(
  id: number,
  patch: {
    display_name?: string;
    role?: UserRole;
    disabled?: boolean;
    password?: string;
  }
): Promise<User> {
  const res = await req(`/api/users/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()).user;
}

export async function deleteUser(id: number): Promise<void> {
  const res = await req(`/api/users/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await readError(res));
}

/** 强制某账号下线（删掉它的全部登录会话） */
export async function logoutAllSessions(id: number): Promise<void> {
  const res = await req(`/api/users/${id}/logout-all`, { method: "POST" });
  if (!res.ok) throw new Error(await readError(res));
}

/**
 * 发送一条消息并消费 SSE 事件流。
 *
 * 帧格式：`data: {...}\n\n`；`: ping` 心跳注释帧直接忽略；
 * 终态事件为 done / error / cancelled，之后服务端关闭连接，本函数随之返回。
 */
export async function chatStream(
  opts: {
    message: string;
    mode: Mode;
    web: boolean;
    session: string;
    workspace?: string;
    attachments?: Attachment[];
    approval?: ApprovalMode;
    /** 会话级模型配置：新会话（磁盘上还没有文件）首条消息时随请求落盘 */
    model_id?: string;
    reasoning?: string;
  },
  onEvent: (ev: SSEEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await req("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(await readError(res));

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    // SSE 帧以空行分隔
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue; // 心跳注释帧在这里被忽略
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          onEvent(JSON.parse(payload) as SSEEvent);
        } catch {
          // 单帧解析失败不致命：跳过，继续处理后续帧
        }
      }
    }
  }
}
