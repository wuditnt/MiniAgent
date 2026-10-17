/**
 * 前端类型定义：与后端 SSE 协议（docs/WEBUI_ANTDX_PLAN.md §6）一一对应。
 */

/** 交互模式：chat = 纯对话；agent = 工具循环 */
export type Mode = "chat" | "agent";

/** 确认模式：ask 按需询问 / auto 自主判断（主动 request_approval）/ full 完全控制 */
export type ApprovalMode = "ask" | "auto" | "full";

/** 沙箱外读取策略：strict 全拦 / whitelist 仅白名单 / open 全部只读放开 */
export type SandboxReadMode = "strict" | "whitelist" | "open";

/** 危险操作确认请求（confirm_request 事件 → ConfirmModal） */
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
  | { type: "notice"; text: string }
  | { type: "cancelled"; reason: string; usage?: Usage }
  | { type: "done"; answer: string; sec: number; usage?: Usage }
  | { type: "error"; message: string; usage?: Usage };

/** 一次工具调用（ThoughtChain 节点）：start 时 ok 未定，end 时补齐 */
export interface ToolCall {
  id: string;
  name: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  sec?: number;
  result?: string;
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
}

/** GET /api/models 返回 */
export interface ModelsInfo {
  providers: ModelProvider[];
  templates: Record<string, ProviderTemplate>;
  active: string;
}

/** POST /api/models/{id}/test 返回 */
export interface TestResult {
  ok: boolean;
  models?: string[];
  error?: string;
  note?: string;
}

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
  /** 沙箱外读取策略；写操作在任何档位下都被硬禁 */
  sandbox_read: SandboxReadMode;
  /** 沙箱外只读白名单（绝对路径，sandbox_read=whitelist 时生效） */
  sandbox_read_whitelist: string[];
}

/** GET/PUT /api/settings 返回 */
export interface SettingsInfo {
  values: GeneralSettings;
  defaults: GeneralSettings;
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
