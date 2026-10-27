/**
 * 前端类型定义：与后端 SSE 协议（docs/WEBUI_ANTDX_PLAN.md §6）一一对应。
 */

/** 交互模式：chat = 纯对话；agent = 工具循环 */
export type Mode = "chat" | "agent";

/** 确认模式：ask 按需询问 / auto 自主判断（主动 request_approval）/ full 完全控制 */
export type ApprovalMode = "ask" | "auto" | "full";

/** 沙箱外读取策略：strict 全拦 / whitelist 仅白名单 / open 全部只读放开 */
export type SandboxReadMode = "strict" | "whitelist" | "open";

/** 推理档位全集（low/high/max，三档都表示开启思考+强度）；各厂家实际支持的档位由模板声明 */
export type ReasoningLevel = "low" | "high" | "max";

/** 危险操作确认请求（confirm_request 事件 → ConfirmPanel） */
export interface ConfirmInfo {
  id: string;
  tool: string;
  args: Record<string, unknown>;
}

/** 附件元数据（由 /api/upload 返回，也在 /api/history 中随用户消息带回） */
export interface Attachment {
  name: string;
  path: string;
  mime: string;
  size: number;
}

/** 一次回答的用量统计（done / error / cancelled 事件带回，为「本轮增量」） */
export interface Usage {
  llm_calls: number;
  tool_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

/** SSE 事件（/api/chat 响应流；终态 = done / error / cancelled） */
export type SSEEvent =
  | { type: "turn"; turn: number; mode: Mode }
  | { type: "delta"; text: string; kind: "content" | "reasoning" }
  | { type: "llm_done"; sec: number; streamed: boolean }
  | { type: "tool_start"; id: string; name: string; args: Record<string, unknown> }
  | { type: "tool_end"; id: string; ok: boolean; sec: number; result: string }
  | { type: "confirm_request"; id: string; tool: string; args: Record<string, unknown> }
  | { type: "ask_request"; id: string; question: string; options: string[]; allow_free_text: boolean; multi: boolean }
  | { type: "notice"; text: string }
  /** 用户显式「请帮我记住 XXX」的写入结果（两种模式都会发） */
  | { type: "memory"; ok: boolean; note: string; duplicate?: boolean; error?: string }
  | { type: "cancelled"; reason: string; usage?: Usage }
  | { type: "done"; answer: string; sec: number; title_pending?: boolean; usage?: Usage }
  | { type: "error"; message: string; usage?: Usage }
  /** 事件流的实时镜像（P0 可选能力）：event 为一条完整轨迹事件信封 */
  | { type: "trace"; event: TraceEvent };

/** 轨迹事件的来源（决定 Trajectory 视图的着色） */
export type TraceSource =
  | "system"
  | "user"
  | "memory"
  | "model"
  | "tool"
  | "approval"
  | "loop";

/** 一条轨迹事件（core/events.py 的 JSONL 信封，与后端一一对应） */
export interface TraceEvent {
  v: number;
  seq: number;
  ts: number;
  /** run_start / user_message / llm_request / tool_call / run_end … */
  type: string;
  source: TraceSource;
  /** run_start / turn / context / run_end */
  phase: string;
  turn: number | null;
  run_id: string;
  payload: Record<string, unknown>;
}

/** GET /api/trajectory 返回 */
export interface TrajectoryData {
  session: string;
  events: TraceEvent[];
  last_seq: number;
}

/** 一次工具调用（ThoughtChain 节点）：start 时 ok 未定，end 时补齐 */
export interface ToolCall {
  id: string;
  name: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  sec?: number;
  result?: string;
}

/** 内联提问卡片的留存记录（回答后 / 失效后折叠展示） */
export interface AskRecord {
  question: string;
  options: string[];
  allowFreeText: boolean;
  /** true = 多选（收集信息）；false/缺省 = 单选（决策） */
  multi: boolean;
  /** 用户答案；空 = 未回答 */
  answer?: string;
  /** true = 超时 / 停止任务导致的失效 */
  expired?: boolean;
}

/** 正在等待用户作答的提问（ask_request 事件驱动，回答后清空） */
export interface PendingAsk extends AskRecord {
  id: string;
}

/** 聊天消息（含实时流式中的 assistant 消息） */
export interface ChatItem {
  id: string;
  role: "user" | "assistant" | "notice";
  content: string;
  status: "streaming" | "done" | "error" | "cancelled";
  reasoning?: string;
  tools?: ToolCall[];
  mode?: Mode;
  sec?: number;
  usage?: Usage;
  error?: string;
  reason?: string;
  /** 用户消息的附件（图片 / 文件），供前端展示 */
  attachments?: Attachment[];
  /** assistant 消息里折叠留存的提问卡片（answered / expired） */
  ask?: AskRecord;
}

/** GET /api/state 返回 */
export interface AgentState {
  model: string;
  model_name: string;
  workspace: string;
  tools: number;
  search_backend: string;
  confirm_timeout: number;
  max_context_chars?: number;
  sessions: { id: string; busy: boolean }[];
  /** true = 当前客户端是远程访问（非回环地址），主机配置类操作被后端禁用 */
  remote?: boolean;
}

/** GET /api/history 返回的单条消息 */
export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  /** assistant 消息的该轮用量与耗时（落盘持久化，老会话可能没有） */
  usage?: Usage;
  sec?: number;
  /** 用户消息的附件元数据 */
  attachments?: Attachment[];
}

/** 会话列表项（GET /api/sessions 返回） */
export interface SessionItem {
  id: string;
  title: string;
  workspace_id: string;
  message_count: number;
  created_at: number;
  updated_at: number;
  /** 搜索命中时：命中第几条消息（-1 = 仅标题命中），供前端跳转定位 */
  hit_index?: number;
  /** 搜索命中时：命中处上下文片段 */
  snippet?: string;
  /** 本会话保存的模型配置 id（空 = 跟全局默认） */
  model_id?: string;
  /** 本会话保存的推理等级（空 = 跟全局默认） */
  reasoning?: ReasoningLevel;
  /** 本会话的联网搜索开关（每个对话独立） */
  web?: boolean;
}

/** 工作区（GET /api/workspaces 返回） */
export interface WorkspaceItem {
  id: string;
  name: string;
  path: string;
  created_at: number;
  session_count: number;
  exists: boolean;
}

/** 工作台：目录条目 */
export interface WorkspaceEntry {
  name: string;
  /** 相对工作区根的路径，统一用 / 分隔 */
  path: string;
  type: "dir" | "file";
  size: number;
  mtime: number;
  ext: string;
}

/** GET /api/workspace/dir */
export interface WorkspaceDirData {
  workspace_id: string;
  root: string;
  path: string;
  parent: string;
  entries: WorkspaceEntry[];
  truncated: boolean;
}

/** GET /api/workspace/file */
export interface WorkspaceFileData {
  kind: "text" | "image" | "pdf" | "binary" | "unsupported" | "mhtml";
  name: string;
  path: string;
  ext: string;
  size: number;
  mtime: number;
  encoding?: string;
  content?: string;
  lines?: number;
  truncated?: boolean;
  reason?: string;
  url?: string;
  /** MHTML：提取后的主 HTML */
  html?: string;
  /** MHTML：资源键 → data URL */
  resources?: Record<string, string>;
}

/** 一份「服务商 + 模型」配置（GET /api/models 返回，API Key 已脱敏） */
export interface ModelProvider {
  id: string;
  provider: string;
  name: string;
  base_url: string;
  model: string;
  api_key_masked: string;
  has_key: boolean;
  is_active: boolean;
  created_at: number;
}

/** 内置服务商模板 */
export interface ProviderTemplate {
  name: string;
  base_url: string;
  models: string[];
  no_key: boolean;
  /** 该厂家支持的推理档位与参数风格；levels 为空 = 不支持思考控制 */
  reasoning?: {
    levels: ReasoningLevel[];
    style: "effort" | "thinking" | "switch";
    /** 界面档位 → 接口实际取值（缺省同名直传，如 OpenAI 无 max → 映射成 high） */
    values?: Record<string, string>;
  };
}

/** GET /api/models 返回 */
export interface ModelsInfo {
  providers: ModelProvider[];
  templates: Record<string, ProviderTemplate>;
  active: string;
  /** 全局推理等级 */
  reasoning: ReasoningLevel;
}

/** POST /api/models/{id}/test 返回 */
export interface TestResult {
  ok: boolean;
  models?: string[];
  error?: string;
  note?: string;
}

/** 联网搜索后端：deepseek 用 DeepSeek 官方 Search API，其余走第三方/网页兜底 */
export type SearchProvider = "deepseek" | "tavily" | "bocha" | "bing";

/** 通用配置（GET /api/settings 返回的 values） */
export interface GeneralSettings {
  max_turns: number;
  max_context_chars: number;
  keep_recent_turns: number;
  max_output_chars: number;
  temperature: number;
  stream: boolean;
  parallel_tools: boolean;
  max_workers: number;
  /** 联网搜索后端（与聊天模型无关，任何厂商的模型都能用） */
  search_provider: SearchProvider;
  /** DeepSeek 联网专用 Key：只写不读（后端恒回空串，已有值时见 secrets） */
  deepseek_search_api_key: string;
  /** 沙箱外读取策略；写操作在任何档位下都被硬禁 */
  sandbox_read: SandboxReadMode;
  /** 沙箱外只读白名单（绝对路径，sandbox_read=whitelist 时生效） */
  sandbox_read_whitelist: string[];
  /** 记忆系统总开关 */
  memory_enabled: boolean;
  /** 每轮结束自动提炼偏好/决定/坑 */
  memory_auto_extract: boolean;
  /** 注入 system prompt 的记忆字符预算 */
  memory_budget_chars: number;
  /** 是否写本地文件日志（logs/ 下按类别分文件） */
  log_enabled: boolean;
  /** 文件日志级别；DEBUG 才能看到 HTTP 访问日志 */
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  /** 日志按天滚动，保留最近 N 天 */
  log_retention_days: number;
  /** 是否把运行时事实写入 <会话>.events.jsonl（运行轨迹抽屉的数据来源） */
  events_enabled: boolean;
}

/** GET /api/memory 返回的三层记忆 */
export interface MemoryData {
  user_profile: string;
  user_profile_path: string;
  project: string;
  project_path: string;
  workspace_root: string;
  topics: MemoryTopic[];
}

export interface MemoryTopic {
  session: string;
  mtime: number;
  mtime_text: string;
  size: number;
  content: string;
}

/** GET/PUT /api/settings 返回 */
export interface SettingsInfo {
  values: GeneralSettings;
  defaults: GeneralSettings;
  /** 只写不读的敏感项脱敏回显：key → "****后四位"（未配置则无此键） */
  secrets?: Record<string, string>;
}

/** 工具信息（GET /api/tools 返回，插件管理界面用） */
export interface ToolInfo {
  name: string;
  description: string;
  group: string;
  source: string;
  module: string;
  dangerous: boolean;
  exclusive: boolean;
  signature: string;
}

// ---------------------------------------------------------------- 登录与账号

/** 账号角色：superadmin 管账号，user 只能用功能 */
export type UserRole = "superadmin" | "user";

/** 一个本地账号（后端永不返回密码哈希） */
export interface User {
  id: number;
  username: string;
  display_name: string;
  role: UserRole;
  disabled: boolean;
  /** 首次登录 / 管理员重置后必须改密，改完才放行其它接口 */
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
}

/** GET /api/auth/me */
export interface AuthMe {
  user: User | null;
  /** --no-login 时为 false（此时 user 是后端给的占位身份） */
  login_enabled: boolean;
}
