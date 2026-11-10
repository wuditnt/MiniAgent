/**
 * 前端类型定义：与后端 SSE 协议（见 docs/02-architecture.md）一一对应。
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

/** 工具产出的图片：本轮生成、已落在工作区里的文件（气泡下方缩略图条） */
export interface Artifact {
  /** 相对工作区根的路径（/ 分隔） */
  path: string;
  name: string;
  ext: string;
  size: number;
  /** 后端拼好的只读访问地址（/api/workspace/raw?...）；老数据可能没有 */
  url?: string;
}

/** 一次回答的用量统计（done / error / cancelled 事件带回，为「本轮增量」） */
export interface Usage {
  llm_calls: number;
  tool_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  /** 前缀缓存命中的 token 数（服务商不支持或老会话可能没有） */
  cached_tokens?: number;
}

/** 执行时间线上的一个动作（工具 / 降级提示 / 起点） */
export interface RunAction {
  /** 相对本轮起点的毫秒偏移 */
  ts: number;
  kind: "step" | "tool" | "notice";
  id?: string | null;
  name: string;
  detail?: string;
  status: "running" | "ok" | "fail" | "info";
  dur_ms?: number | null;
}

/** 一轮执行记录：起止时间由后端写入，前端只渲染（关页/重连不丢） */
export interface RunInfo {
  /** 第几个用户轮次（1 起），前端据此把 run 挂到对应气泡 */
  turn: number;
  started_at: number;
  ended_at?: number | null;
  elapsed_ms?: number | null;
  status: "running" | "done" | "cancelled" | "error";
  actions: RunAction[];
}

/** SSE 事件（/api/chat 响应流；终态 = done / error / cancelled）
 *  · session：共享多路复用流里标识事件属于哪个会话
 *  · seq：服务端全局单调序号，用于去重与断线续传（Last-Event-ID）
 */
export type SSEEvent = { session?: string; seq?: number } & (
  | { type: "turn"; turn: number; mode: Mode }
  | { type: "run_start"; turn: number; started_at: number; actions: RunAction[]; server_now: number }
  | { type: "delta"; text: string; kind: "content" | "reasoning" }
  | { type: "llm_done"; sec: number; streamed: boolean }
  | { type: "tool_start"; id: string; name: string; args: Record<string, unknown>; ts?: number }
  | { type: "tool_end"; id: string; ok: boolean; sec: number; result: string; ts?: number }
  /** 工具产出的图片（已落盘到工作区）：前端在气泡下方追加缩略图 */
  | { type: "artifact"; items: Artifact[] }
  | { type: "confirm_request"; id: string; tool: string; args: Record<string, unknown> }
  | { type: "ask_request"; id: string; question: string; options: string[]; allow_free_text: boolean; multi: boolean }
  | { type: "notice"; text: string; ts?: number }
  /** 用户显式「请帮我记住 XXX」的写入结果（两种模式都会发） */
  | { type: "memory"; ok: boolean; note: string; duplicate?: boolean; error?: string }
  | { type: "cancelled"; reason: string; usage?: Usage; elapsed_ms?: number | null }
  | { type: "done"; answer: string; sec: number; title_pending?: boolean; usage?: Usage; elapsed_ms?: number | null }
  | { type: "error"; message: string; usage?: Usage; elapsed_ms?: number | null }
  /** 事件流的实时镜像（P0 可选能力）：event 为一条完整轨迹事件信封 */
  | { type: "trace"; event: TraceEvent }
  /** 接入后台任务时的首帧：当前全部消息 + 是否仍在运行 + 当前这轮的时间线 */
  | {
      type: "snapshot";
      session: string;
      messages: HistoryMessage[];
      runs?: RunInfo[];
      run?: RunInfo | null;
      server_now?: number;
      running: boolean;
      /** 已经流式生成、但还没定稿的正文/思考（重连时补进流式气泡） */
      live?: { content: string; reasoning: string };
    }
);

/** 全局事件流（/api/events）广播的控制信号：不含正文，只驱动列表刷新/历史重拉 */
export type GlobalEvent =
  | { type: "session_created"; session: string; workspace_id?: string; source?: string }
  | { type: "run_started"; session: string; source?: string }
  | { type: "run_finished"; session: string; status: string };

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
  /** true = 停止任务 / 连接断开导致的失效（本系统不设超时） */
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
  /** 本轮工具生成的图片（保留在气泡下方，历史回放同样可见） */
  artifacts?: Artifact[];
  /** 消息时间（秒级时间戳）：工具栏里展示「今天 23:08」 */
  ts?: number;
  /** assistant 消息里折叠留存的提问卡片（answered / expired） */
  ask?: AskRecord;
  /** 本轮执行记录（后端时间线）：计时、最后动作、可展开的动作列表 */
  run?: RunInfo;
}

/** GET /api/state 返回 */
export interface AgentState {
  model: string;
  model_name: string;
  workspace: string;
  tools: number;
  search_backend: string;
  max_context_chars?: number;
  sessions: { id: string; busy: boolean }[];
  /** true = 当前客户端是远程访问（非回环地址），主机配置类操作被后端禁用 */
  remote?: boolean;
  /** 后端版本号（唯一真源：src/core/__init__.py 的 __version__） */
  version: string;
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
  /** 该轮工具产出的图片（老会话可能没有） */
  artifacts?: Artifact[];
  /** 消息时间（秒级时间戳），老会话可能没有 */
  ts?: number;
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
  /** 本会话勾选的连接器名；null/缺省 = 跟全局默认 */
  connectors?: string[] | null;
  /** 本会话的模式：chat=快速对话 / agent=智能体（随每轮消息落盘） */
  mode?: Mode;
  /** 来源："" = 用户会话；automation = 自动化任务创建 */
  source?: string;
  /** 归属的自动化任务 id（用户会话为空） */
  automation_id?: string;
  /** 自动化跑完且尚未查看（进入会话即清除） */
  unread?: boolean;
  /** 是否正在执行（来自后端内存状态，侧栏据此显示运行指示器） */
  running?: boolean;
}

// ---------------------------------------------------------------- 自动化任务

/** 执行规则（与后端 schedule_json 同构） */
export type AutomationSchedule =
  | { kind: "once"; at: number }
  | { kind: "daily"; time: string }
  | { kind: "weekly"; weekdays: number[]; time: string }
  | { kind: "interval"; every_minutes: number };

export type AutomationScheduleKind = AutomationSchedule["kind"];

/** GET/POST/PUT /api/automations 返回的任务 */
export interface AutomationTask {
  id: string;
  name: string;
  prompt: string;
  workspace_id: string;
  session_id: string;
  schedule: AutomationSchedule;
  enabled: boolean;
  web: boolean;
  model_id: string;
  created_at: number;
  updated_at: number;
  next_run_at: number;
  last_run_at: number | null;
  last_status: string;
  last_error: string;
  run_count: number;
  missed: number;
  /** 规则的中文摘要（后端生成） */
  schedule_text: string;
  /** 接下来三次执行时间（epoch 秒） */
  next_runs: number[];
}

export interface AutomationsData {
  tasks: AutomationTask[];
  running: Record<string, number>;
  server_now: number;
}

/** AI 助手允许修改的字段白名单 */
export interface AutomationPatch {
  name?: string;
  prompt?: string;
  workspace_id?: string;
  schedule?: AutomationSchedule;
  web?: boolean;
  model_id?: string;
  enabled?: boolean;
}

/** 编辑器表单草稿（AI 助手以它作为「眼前所见」的基准） */
export interface AutomationDraft {
  name: string;
  prompt: string;
  workspace_id: string;
  schedule: AutomationSchedule;
  web: boolean;
  model_id: string;
  enabled: boolean;
}

export interface AutomationChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  patch?: AutomationPatch | null;
  created_at: number;
}

export interface AutomationAssistResult {
  reply: string;
  patch: AutomationPatch;
  warnings: string[];
  questions: string[];
  preview: number[];
  server_now: number;
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

/** CSV / TSV 表格的一个「行窗口 × 列窗口」 */
export interface WorkspaceTablePage {
  columns: string[];
  rows: string[][];
  row_offset: number;
  row_limit: number;
  col_offset: number;
  col_limit: number;
  total_rows: number;
  total_cols: number;
  encoding: string;
  delimiter: string;
  /** 命中 20MB / 10000 行 / 1000 列解析上限 */
  truncated: boolean;
}

/** GET /api/workspace/file */
export interface WorkspaceFileData {
  kind: "text" | "image" | "pdf" | "table" | "binary" | "unsupported" | "mhtml";
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
  /** 表格：首屏窗口数据（后续翻页走 /api/workspace/table） */
  table?: WorkspaceTablePage;
  /** MHTML：提取后的主 HTML */
  html?: string;
  /** MHTML：资源键 → data URL */
  resources?: Record<string, string>;
}

/** GET /api/skills：一个可用技能 */
export interface SkillItem {
  /** 稳定标识：`<source>:<目录名>`，如 custom:my-skill（启停、编辑、删除都用它） */
  id: string;
  name: string;
  description: string;
  /** builtin=系统内置（skills/）；custom=用户自己创建（skills_user/ 等） */
  source: "builtin" | "custom";
  /** 停用后不进 system prompt 清单、不出现在输入框选择器 */
  enabled: boolean;
  /** 仅自定义技能可编辑 / 删除 */
  editable: boolean;
}

/** GET /api/skills/detail：技能元信息 + SKILL.md 原文 */
export interface SkillDetail {
  skill: SkillItem;
  /** 原文（编辑器用，含 frontmatter） */
  content: string;
  /** 去掉 frontmatter 的正文（Markdown 渲染用） */
  body: string;
  /** 展示用目录路径（项目内为相对路径） */
  path: string;
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
  /** 智能体模式的工具档位：standard=只加载常用工具（省 token），full=全部工具 */
  tool_profile: "standard" | "full";
  /** 子智能体（委派）：开启后 delegate 工具进会话工具表 */
  subagent_enabled: boolean;
  /** 快速对话（chat 模式）放行本地只读工具（查时间 / 本机状态 / 端口 / 剪贴板 / 列目录） */
  chat_tools: boolean;
  /** 单个子任务最多跑几轮 */
  subagent_max_turns: number;
  /** 单个子任务的墙钟超时（秒） */
  subagent_timeout: number;
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

/** 工具信息（GET /api/tools 返回，工具管理界面用） */
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

// ---------------------------------------------------------------- 桌面壳

/** 桌面后端连接信息（preload 的 desktop:backend） */
export interface DesktopBackendInfo {
  port: number;
  version: string;
  lanEnabled: boolean;
  dataDir: string;
  logFile: string;
}

/** desktop-config.json 的可见部分 */
export interface DesktopConfig {
  lan_enabled: boolean;
  window: { width: number; height: number };
}

/**
 * Electron preload 注入到 window.__DESKTOP__ 的白名单能力。
 * 浏览器下该对象不存在，前端一律走原有 Web 逻辑。
 */
export interface DesktopBridge {
  isDesktop: true;
  platform: string;
  backend(): Promise<DesktopBackendInfo>;
  getConfig(): Promise<DesktopConfig>;
  setLanEnabled(
    enabled: boolean
  ): Promise<DesktopConfig & { restart_required: boolean }>;
  restart(): Promise<void>;
  openLogs(): Promise<void>;
}

/** GET /api/health 的关键字段（桌面端状态 / 局域网地址展示用） */
export interface HealthInfo {
  ready: boolean;
  mode: "desktop" | "web";
  host: string;
  port: number;
  lan_enabled: boolean;
  lan_urls: string[];
  data_dir: string;
  uptime: number;
}

// ---------------------------------------------------------------- 连接器（MCP）

/** 连接器暴露的一个工具 */
export interface ConnectorTool {
  name: string;
  /** 服务端原始工具名（工具级开关用这个名字） */
  raw_name?: string;
  description: string;
  dangerous?: boolean;
  /** false = 被 disabled_tools 关闭，未注册给模型 */
  enabled?: boolean;
}

/** 已配置的连接器（GET /api/connectors） */
export interface ConnectorInfo {
  name: string;
  display_name: string;
  description: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  url: string;
  cwd: string;
  env: Record<string, string>;
  /** 自定义请求头（值为掩码） */
  headers: Record<string, string>;
  timeout: number;
  enabled: boolean;
  dangerous: boolean;
  /** 被禁用的工具（服务端原始工具名） */
  disabled_tools: string[];
  /** 启动命令当前是否能在 PATH 中找到（python 视为可用） */
  command_found: boolean;
  /** ready / failed / stopped */
  status: string;
  error: string;
  server_info: Record<string, unknown>;
  tools: ConnectorTool[];
  /** 已注册（启用）的工具数 / 服务端发现的总数 */
  tool_count: number;
  tool_total: number;
  /** 最近一次启动耗时（毫秒）与尝试次数 */
  started_ms: number;
  attempts: number;
  /** 启动失败时子进程 stderr 的尾部 */
  stderr_tail: string[];
  /** 服务端通知日志（notifications/message） */
  logs: string[];
}

/** 宿主机运行环境（决定 npx 类连接器是否可用） */
export interface ConnectorRuntime {
  node: { found: boolean; path: string; version: string };
  npx: { found: boolean; path: string };
}

export interface ConnectorsData {
  connectors: ConnectorInfo[];
  runtime: ConnectorRuntime;
  mcp_enabled: boolean;
  running: number;
  cancel_stats?: Record<string, unknown>;
  /** 全局默认勾选；null = 未设置（新建会话继承全部已启用项） */
  defaults?: string[] | null;
}

/** 新增/编辑连接器提交的字段 */
export interface ConnectorSpec {
  name: string;
  display_name?: string;
  description?: string;
  transport?: "stdio" | "http";
  command: string;
  args?: string[];
  url?: string;
  headers?: Record<string, string>;
  cwd?: string;
  env?: Record<string, string>;
  timeout?: number;
  enabled?: boolean;
  dangerous?: boolean;
  disabled_tools?: string[];
}

export interface ConnectorTestResult {
  ok: boolean;
  error?: string;
  stderr?: string[];
  server_info?: Record<string, unknown>;
  tools?: { name: string; description: string }[];
}

/** 内置模板（GET /api/connectors/presets） */
export interface ConnectorPreset {
  key: string;
  display_name: string;
  description: string;
  command: string;
  args: string[];
  dangerous: boolean;
  hint: string;
  docs: string;
}

/** 导入结果（POST /api/connectors/import） */
export interface ConnectorImportResult {
  ok: boolean;
  results: { name: string; ok: boolean; error?: string }[];
  warnings: string[];
}

declare global {
  interface Window {
    __DESKTOP__?: DesktopBridge;
  }
}

