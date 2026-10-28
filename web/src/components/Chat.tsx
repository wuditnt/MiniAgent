/**
 * 消息区 + 输入区：
 *  · assistant 气泡 = 思考过程（可折叠）+ Markdown 正文 + ThoughtChain 工具轨迹 + 状态脚注
 *  · 流式效果来自 items 增量更新，不靠打字机动画
 *  · Sender 的 loading 态自动把发送按钮变成「停止」
 */
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ElementRef } from "react";
import {
  CheckOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
  MoreOutlined,
  PaperClipOutlined,
  PictureOutlined,
  PlusOutlined,
  RedoOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UpOutlined,
} from "@ant-design/icons";
import { Bubble, Sender } from "@ant-design/x";
import { Button, Checkbox, Collapse, Dropdown, Input, message, Radio, Tag, Tooltip } from "antd";
import type { MenuProps } from "antd";
import type {
  ApprovalMode,
  AskRecord,
  Attachment,
  ChatItem,
  ConfirmInfo,
  Mode,
  ModelProvider,
  PendingAsk,
  ReasoningLevel,
  SkillItem,
  WorkspaceItem,
} from "../types";
import { fetchSkills, uploadFile } from "../api";
import Markdown from "./Markdown";
import ToolChain from "./ToolChain";
import WelcomePane from "./WelcomePane";
import SkillInput from "./composer/SkillInput";
import SkillPicker from "./composer/SkillPicker";
import { hasSkillToken, parseComposer, skillToken } from "./composer/skillMarks";
import { detectSlashCommand, stripTrigger } from "./composer/slashCommands";
import ConfirmPanel from "./ConfirmPanel";

/** token 数缩写：59300 → 59.3K（与参考图一致） */
function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

/** 耗时：12.3s / 1m23s */
function fmtSec(sec: number): string {
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}

/** 降级复制：某些环境（无 user activation / 非安全上下文）拿不到 clipboard API */
function fallbackCopy(text: string): boolean {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** 把 Markdown 文本还原为纯文本（去掉语法标记，保留可读内容） */
function stripMarkdown(md: string): string {
  return md
    // 代码块：保留内容，去掉围栏和语言标记
    .replace(/```[\w]*\n([\s\S]*?)```/g, "$1")
    // 行内代码
    .replace(/`([^`]+)`/g, "$1")
    // 图片和链接：只保留 alt / 文字
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    // 粗体、斜体、删除线
    .replace(/\*\*([\s\S]*?)\*\*/g, "$1")
    .replace(/\*([\s\S]*?)\*/g, "$1")
    .replace(/__([\s\S]*?)__/g, "$1")
    .replace(/_([\s\S]*?)_/g, "$1")
    .replace(/~~([\s\S]*?)~~/g, "$1")
    // 标题
    .replace(/^#{1,6}\s*/gm, "")
    // 引用
    .replace(/^\s*>\s*/gm, "")
    // 无序 / 有序列表标记
    .replace(/^[-*+]\s+/gm, "")
    .replace(/^\d+\.\s+/gm, "")
    // 分割线
    .replace(/^\s*[-*_]{3,}\s*$/gm, "")
    // 数学公式标记（保留公式内容）
    .replace(/\$\$([\s\S]*?)\$\$/g, "$1")
    .replace(/\$([^$]+)\$/g, "$1")
    // 表格：把竖线变空格，去掉对齐分隔行
    .replace(/^\|(.+)\|\s*$/gm, (_m, inner: string) =>
      inner.split("|").map((s) => s.trim()).filter(Boolean).join("  ")
    )
    .replace(/^\s*\|[\s\-:|]+\|\s*$/gm, "")
    // 压缩多余空行
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/**
 * 每轮回答下方的小工具栏：复制文本 / 用量统计 / 耗时。
 * 只有拿到数据的项才显示 —— 用量与耗时随历史落盘持久化，
 * 老会话（保存该能力之前产生的）没有这两项，不占位。
 *
 * 复制反馈走命令式 DOM（data-state + CSS），不依赖 React 重渲染：
 * 这类「一次性提示」用 DOM 属性最稳，也不会被父级 memo 影响。
 */
function MessageToolbar({
  item,
  onRegenerate,
  onDelete,
  onTrajectory,
}: {
  item: ChatItem;
  onRegenerate?: (msgId: string) => void;
  onDelete?: (msgId: string) => void;
  /** 打开本会话的运行轨迹抽屉（P0 事实层） */
  onTrajectory?: () => void;
}) {
  const btnRef = useRef<HTMLSpanElement>(null);
  const hasContent = !!item.content.trim();
  const usage = item.usage;
  const showUsage = !!usage && usage.total_tokens > 0;
  const showSec = item.sec != null;

  if (!hasContent && !showUsage && !showSec && !onRegenerate && !onTrajectory) return null;

  const mark = (ok: boolean) => {
    const el = btnRef.current;
    if (!el) return;
    el.dataset.state = ok ? "ok" : "fail";
    el.dataset.copies = String(Number(el.dataset.copies || 0) + 1);
    setTimeout(() => {
      if (el.dataset.state) delete el.dataset.state;
    }, 1500);
  };

  const copy = async (text: string) => {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        // 加超时竞速：某些环境下写剪贴板既不 resolve 也不 reject，会把 await 挂死
        ok = await Promise.race([
          navigator.clipboard.writeText(text).then(() => true),
          new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 400)),
        ]);
      }
    } catch {
      ok = false;     // 没权限 / 没有用户手势 → 走降级
    }
    if (!ok) ok = fallbackCopy(text);
    mark(ok);
  };

  return (
    <div className="msg-toolbar">
      {hasContent && (
        <Dropdown
          trigger={["hover"]}
          placement="topLeft"
          menu={{
            items: [
              { key: "markdown", label: "复制为 Markdown" },
              { key: "plain", label: "复制为纯文本" },
            ],
            onClick: ({ key }) => {
              if (key === "plain") copy(stripMarkdown(item.content));
              else copy(item.content);
            },
          }}
        >
          <span ref={btnRef} className="msg-tool">
            <CopyOutlined className="icon-copy" />
            <CheckOutlined className="icon-check" />
          </span>
        </Dropdown>
      )}
      {onTrajectory && (
        <Tooltip title="查看运行轨迹">
          <span className="msg-tool" onClick={onTrajectory}>
            <HistoryOutlined />
          </span>
        </Tooltip>
      )}
      {onRegenerate && (
        <Tooltip title="重新生成">
          <span className="msg-tool" onClick={() => onRegenerate(item.id)}>
            <RedoOutlined />
          </span>
        </Tooltip>
      )}
      {onDelete && (
        <Tooltip title="删除此条及之后消息">
          <span className="msg-tool" onClick={() => onDelete(item.id)}>
            <DeleteOutlined />
          </span>
        </Tooltip>
      )}
      <span className="msg-toolbar-spacer" />
      {showUsage && (
        <Tooltip
          title={`输入 ${usage.prompt_tokens} / 输出 ${usage.completion_tokens} tokens ｜ 模型调用 ${usage.llm_calls} 次 ｜ 工具 ${usage.tool_calls} 次`}
        >
          <span className="msg-stat">
            <DatabaseOutlined />
            用量 {fmtTokens(usage.total_tokens)} tok
          </span>
        </Tooltip>
      )}
      {showSec && (
        <Tooltip title="本轮耗时">
          <span className="msg-stat">{fmtSec(item.sec as number)}</span>
        </Tooltip>
      )}
    </div>
  );
}

/** 内联提问卡片里「其他」选项的内部取值（不会与真实候选冲突） */
const OTHER = "\u0000other";

/**
 * 内联提问卡片：ask_user 工具让 Agent 在助手气泡里直接摆出问题与候选。
 * 两种形态：
 *  · pending：可交互 —— 单选（multi=false）/ 多选（multi=true）+ 可选「其他」自由输入，点提交即回传；
 *  · record：只读留存 —— 已回答显示所选，超时/停止显示「未回答」。
 */
function AskCard({
  pending,
  record,
  onAnswer,
}: {
  pending?: PendingAsk | null;
  record?: AskRecord | null;
  onAnswer?: (answer: string) => void;
}) {
  const [choice, setChoice] = useState("");
  const [choices, setChoices] = useState<string[]>([]);
  const [custom, setCustom] = useState("");
  const src = pending || record;
  if (!src) return null;

  // ---- 只读留存（本轮已结束，卡片折回气泡）
  if (!pending) {
    const answered = !!src.answer;
    return (
      <div className={`ask-card ask-card-settled${answered ? "" : " ask-card-expired"}`}>
        <span className="ask-settled-label">{answered ? "已选择" : "未回答"}</span>
        <span className="ask-settled-value">
          {answered ? src.answer : "（超时或任务已停止，Agent 按未回答继续）"}
        </span>
      </div>
    );
  }

  const options = src.options || [];
  const freeOk = src.allowFreeText;
  const multi = !!src.multi;
  const useCustom = options.length === 0 || choice === OTHER;
  const answer = (
    multi
      ? [...choices, ...(custom.trim() ? [custom.trim()] : [])].join("、")
      : (useCustom ? custom : choice)
  ).trim();

  const submit = () => {
    if (answer) onAnswer?.(answer);
  };

  return (
    <div className="ask-card">
      <div className="ask-question">{src.question}</div>
      {options.length > 0 &&
        (multi ? (
          <Checkbox.Group
            className="ask-options"
            value={choices}
            onChange={(vals) => setChoices(vals as string[])}
          >
            {options.map((o) => (
              <Checkbox key={o} value={o}>
                {o}
              </Checkbox>
            ))}
            {freeOk && <Checkbox value={OTHER}>其他</Checkbox>}
          </Checkbox.Group>
        ) : (
          <Radio.Group
            className="ask-options"
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
          >
            {options.map((o) => (
              <Radio key={o} value={o}>
                {o}
              </Radio>
            ))}
            {freeOk && <Radio value={OTHER}>其他</Radio>}
          </Radio.Group>
        ))}
      {freeOk && (
        <Input
          className="ask-input"
          size="small"
          value={custom}
          placeholder={options.length ? "或直接输入你的答案" : "输入你的答案"}
          onChange={(e) => setCustom(e.target.value)}
          onFocus={() => {
            if (!multi && options.length) setChoice(OTHER);
          }}
          onPressEnter={submit}
          disabled={!multi && options.length > 0 && choice !== OTHER}
        />
      )}
      <Button
        className="ask-submit"
        type="primary"
        size="small"
        disabled={!answer}
        onClick={submit}
      >
        提交
      </Button>
    </div>
  );
}

function AssistantBodyInner({
  item,
  onRegenerate,
  onDelete,
  onTrajectory,
  workspaceId,
  pendingAsk,
  onAnswerAsk,
}: {
  item: ChatItem;
  onRegenerate?: (msgId: string) => void;
  onDelete?: (msgId: string) => void;
  onTrajectory?: () => void;
  workspaceId: string;
  pendingAsk?: PendingAsk | null;
  onAnswerAsk?: (answer: string) => void;
}) {
  return (
    <div className="assistant-body">
      {item.reasoning ? (
        <Collapse
          ghost
          size="small"
          className="reasoning"
          items={[
            {
              key: "r",
              label: "思考过程",
              children: <div className="reasoning-text">{item.reasoning}</div>,
            },
          ]}
        />
      ) : null}
      {item.tools && item.tools.length > 0 ? (
        <Collapse
          ghost
          size="small"
          className="tools-collapse"
          defaultActiveKey={[]}
          items={[
            {
              key: "t",
              label: (
                <div className="tools-header">
                  <span className="tools-header-title">工具调用</span>
                  <span className="tools-header-names">
                    {item.tools.map((t) => t.name).join(" · ")}
                  </span>
                  <span className="tools-header-count">共 {item.tools.length} 个</span>
                </div>
              ),
              children: <ToolChain tools={item.tools} />,
            },
          ]}
        />
      ) : null}
      {item.content ? <Markdown text={item.content} workspaceId={workspaceId} /> : null}
      {item.ask ? (
        <AskCard record={item.ask} />
      ) : item.status === "streaming" && pendingAsk ? (
        <AskCard pending={pendingAsk} onAnswer={onAnswerAsk} />
      ) : null}
      {item.status === "error" ? (
        <div className="status-error">出错了：{item.error || "未知错误"}</div>
      ) : null}
      {item.status === "cancelled" ? (
        <Tag color="orange" style={{ marginTop: 6 }}>
          已停止{item.reason ? `：${item.reason}` : ""}
        </Tag>
      ) : null}
      {item.status !== "streaming" ? (
        <MessageToolbar
          item={item}
          onRegenerate={onRegenerate}
          onDelete={onDelete}
          onTrajectory={onTrajectory}
        />
      ) : null}
    </div>
  );
}

/** 历史消息的 item 对象不变 → 跳过整棵子树（Markdown / 工具轨迹 / KaTeX） */
const AssistantBody = memo(AssistantBodyInner);

interface Props {
  items: ChatItem[];
  busy: boolean;
  mode: Mode;
  onMode: (m: Mode) => void;
  /** 三档确认模式（ask / auto / full），仅智能体模式下可切 */
  approval: ApprovalMode;
  onApproval: (m: ApprovalMode) => void;
  onSend: (text: string, attachments?: Attachment[], skills?: string[]) => void;
  onStop: () => void;
  /** 搜索命中后要定位闪烁的消息 id（形如 h-3）；为 null 表示无定位请求 */
  focusId?: string | null;
  /** 工作区选择：新对话可切换，已开始的对话锁定为只读 */
  workspaces: WorkspaceItem[];
  workspaceId: string;
  onWorkspaceChange: (id: string) => void;
  workspaceLocked: boolean;
  /** 上下文水位：已用字符数 / 上限 */
  contextUsage?: number;
  contextLimit?: number;
  /** 重新生成（assistant 消息）：截掉该条及之后的内容，重发对应的 user 消息 */
  onRegenerate?: (msgId: string) => void;
  /** 编辑重发（user 消息）：截掉该条及之后的内容，把原文填回输入框 */
  onEditResend?: (msgId: string) => void;
  /** 删除消息：截掉该条及之后的内容 */
  onDelete?: (msgId: string) => void;
  /** 打开本会话的运行轨迹抽屉（P0 事实层） */
  onTrajectory?: () => void;
  /** 内联提问：正在等待作答的 ask_user 卡片（渲染在当前流式气泡内） */
  pendingAsk?: PendingAsk | null;
  onAnswerAsk?: (answer: string) => void;
  /** 权限审批：正在等待作答的确认请求（覆盖在输入框位置上） */
  confirm?: ConfirmInfo | null;
  onAnswerConfirm?: (approve: boolean) => void;
  /** 审批等待上限（秒），来自 /api/state 的 confirm_timeout */
  confirmTimeout?: number;
  /** 模型选择器：列表 / 服务商展示名映射 / 当前激活 id / 当前模型名 */
  models: ModelProvider[];
  providerNames: Record<string, string>;
  activeModelId?: string;
  modelName?: string;
  /** 本会话的推理等级（仅智能体模式生效） */
  reasoning: ReasoningLevel;
  /** 当前模型厂家支持的推理档位（空数组 = 该厂家不支持推理配置） */
  reasoningLevels: ReasoningLevel[];
  onActivateModel: (id: string) => void;
  onSetReasoning: (level: ReasoningLevel) => void;
  /** 打开「模型」设置页 */
  onManageModels: () => void;
}

/** 推理等级的展示文案（三档：low / high / max） */
const LEVEL_LABEL: Record<ReasoningLevel, string> = {
  low: "Low",
  high: "High",
  max: "Max",
};

/**
 * 长会话分段渲染：默认只渲染最近 INITIAL_VISIBLE 条，向上滚动或点按钮再往前加载，
 * 避免一次性解析上百条 Markdown（以及 KaTeX / 图表）把首屏和输入拖慢。
 */
const INITIAL_VISIBLE = 30;
const LOAD_STEP = 30;

/**
 * 输入框上方左侧的工作区胶囊：告诉用户「这条对话的文件操作边界在哪」。
 * 新对话可点开切换（未选择时落在默认工作区）；已开始的对话只读展示。
 */
function WorkspaceChip({
  workspaces,
  workspaceId,
  locked,
  onChange,
}: {
  workspaces: WorkspaceItem[];
  workspaceId: string;
  locked: boolean;
  onChange: (id: string) => void;
}) {
  const current =
    workspaces.find((w) => w.id === workspaceId) ||
    workspaces.find((w) => w.id === "default") ||
    workspaces[0];

  const chip = (
    <span className={`ws-chip ${locked ? "locked" : ""}`}>
      <FolderOpenOutlined />
      <span className="ws-chip-name">{current?.name || "默认工作区"}</span>
      {!locked && <DownOutlined className="ws-chip-caret" />}
    </span>
  );

  if (locked || workspaces.length === 0) {
    return (
      <Tooltip title={`本条对话已在「${current?.name || "默认工作区"}」下运行，如需换工作区请新建对话`}>
        {chip}
      </Tooltip>
    );
  }

  return (
    <Dropdown
      trigger={["click"]}
      menu={{
        selectedKeys: [workspaceId],
        items: workspaces.map((w) => ({
          key: w.id,
          label: (
            <div className="ws-menu-item">
              <div className="ws-menu-name">{w.name}</div>
              <div className="ws-menu-path">{w.path}</div>
            </div>
          ),
        })),
        onClick: ({ key }) => onChange(key),
      }}
    >
      <span className="ws-chip-trigger">{chip}</span>
    </Dropdown>
  );
}

/** 三档确认模式的元数据：短名（胶囊）/ 全名 + 说明（菜单） */
const APPROVAL_OPTIONS: { key: ApprovalMode; short: string; name: string; desc: string }[] = [
  { key: "ask", short: "按需询问", name: "按需询问", desc: "危险操作执行前逐个弹窗确认" },
  { key: "auto", short: "自主判断", name: "自主判断", desc: "模型按风险准则自行判断，必要时主动申请授权" },
  { key: "full", short: "完全控制", name: "完全控制", desc: "沙箱内直接执行不再弹窗（MCP 工具仍会确认）" },
];

/**
 * 输入框工具栏的确认模式盾牌胶囊：
 * ask 蓝 / auto 青 / full 琥珀，颜色随风险递增，hover 出菜单切换。
 */
function ApprovalChip({
  value,
  onChange,
}: {
  value: ApprovalMode;
  onChange: (m: ApprovalMode) => void;
}) {
  const current = APPROVAL_OPTIONS.find((o) => o.key === value) || APPROVAL_OPTIONS[0];
  return (
    <Dropdown
      trigger={["click"]}
      placement="topLeft"
      menu={{
        selectedKeys: [value],
        items: APPROVAL_OPTIONS.map((o) => ({
          key: o.key,
          label: (
            <div className="approval-menu-item">
              <div className="approval-menu-name">{o.name}</div>
              <div className="approval-menu-desc">{o.desc}</div>
            </div>
          ),
        })),
        onClick: ({ key }) => onChange(key as ApprovalMode),
      }}
    >
      <span className={`tb-approval tb-approval-${value}`} title={current.name}>
        <SafetyCertificateOutlined />
        <span className="tb-approval-text">{current.short}</span>
        <DownOutlined className="tb-mode-caret" />
      </span>
    </Dropdown>
  );
}

/** 附件大小缩写：1.2K / 3.4M */
function fmtSize(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`;
  return `${(n / (1024 * 1024)).toFixed(1)}M`;
}

/** 用户消息里的附件展示：图片渲染缩略图，其它显示文件 chip */
function AttachmentPreview({
  attachments,
  workspaceId,
}: {
  attachments?: Attachment[];
  workspaceId: string;
}) {
  if (!attachments || !attachments.length) return null;
  return (
    <div className="msg-atts">
      {attachments.map((a, i) =>
        a.mime.startsWith("image/") ? (
          <a
            key={i}
            href={`/api/attachment?path=${encodeURIComponent(a.path)}&workspace=${encodeURIComponent(workspaceId)}`}
            target="_blank"
            rel="noreferrer"
          >
            <img
              className="msg-att-img"
              src={`/api/attachment?path=${encodeURIComponent(a.path)}&workspace=${encodeURIComponent(workspaceId)}`}
              alt={a.name}
            />
          </a>
        ) : (
          <span key={i} className="msg-att-chip" title={a.path}>
            <PaperClipOutlined />
            <span className="msg-att-name">{a.name}</span>
            <span className="msg-att-size">{fmtSize(a.size)}</span>
          </span>
        )
      )}
    </div>
  );
}

export default function Chat({
  items,
  busy,
  mode,
  onMode,
  approval,
  onApproval,
  onSend,
  onStop,
  focusId,
  workspaces,
  workspaceId,
  onWorkspaceChange,
  workspaceLocked,
  onRegenerate,
  onEditResend,
  onDelete,
  onTrajectory,
  contextUsage,
  contextLimit,
  pendingAsk,
  onAnswerAsk,
  confirm,
  onAnswerConfirm,
  confirmTimeout,
  models,
  providerNames,
  activeModelId,
  modelName,
  reasoning,
  reasoningLevels,
  onActivateModel,
  onSetReasoning,
  onManageModels,
}: Props) {
  const [input, setInput] = useState("");
  const senderRef = useRef<ElementRef<typeof Sender>>(null);
  // 技能选择：清单懒加载（首次点开或输入 "/" 时才请求）
  const [skills, setSkills] = useState<SkillItem[] | null>(null);
  const [skillName, setSkillName] = useState<string | null>(null);
  const [skillOpen, setSkillOpen] = useState(false);
  // 触发本次面板的 "/" 在输入串里的位置（选中技能后要把它删掉）
  const [slashAt, setSlashAt] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevLen = useRef(0);
  // 上一轮的决策卡片：用于在卡片出现的瞬间强制滚到底部
  const prevAsk = useRef<PendingAsk | null>(null);
  // 已上传、待发送的附件（chips 展示在输入框上方）
  const [pending, setPending] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  // 当前渲染的历史窗口（条数，从最新往前数）；长会话分段加载
  const [visibleCount, setVisibleCount] = useState(INITIAL_VISIBLE);
  // 消息区是否已可滚动：可滚动时靠「滚到顶部」自动加载，否则才显示按钮
  const [scrollable, setScrollable] = useState(false);
  // 加载更早消息时记录滚动位置，渲染后按高度差补偿，避免视口跳动
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const hiddenCount = Math.max(0, items.length - visibleCount);

  /** 技能清单只在真正需要时拉一次（点开选择器 / 输入 "/"） */
  const ensureSkills = () => {
    if (skills !== null) return;
    fetchSkills()
      .then(setSkills)
      .catch(() => setSkills([]));
  };

  /**
   * 输入变化：
   *  · 命中斜杠指令（行首或空格后的 "/"）→ 弹出对应选择器，触发符本身保留；
   *  · 之后继续输入（或删掉触发符）→ 选择器立即收起，不打扰正常打字。
   * 触发判定统一走 composer/slashCommands，以后加新指令只改那个注册表。
   */
  const handleInputChange = (v: string) => {
    if (!hasSkillToken(v)) setSkillName(null);
    const hit = detectSlashCommand(input, v);
    if (hit && hit.command.id === "skill" && !busy) {
      ensureSkills();
      setSkillOpen(true);
      setSlashAt(hit.index);
    } else if (skillOpen) {
      setSkillOpen(false);
      setSlashAt(null);
    }
    setInput(v);
  };

  /**
   * 选中技能：输入框为空时技能块在开头，已有文字时接在文字后面；
   * 技能块与正文之间自动补一个空格，避免粘在一起。
   * 斜杠触发的情况下先删掉那个 "/"：行首的把它变成「技能块 + 后续文字」。
   */
  const applySkill = (name: string) => {
    const raw = slashAt === null ? input : stripTrigger(input, slashAt);
    const leading = slashAt === 0;
    const { text } = parseComposer(raw);
    const body = text.trim();
    const token = skillToken(name);
    const next = !body ? `${token} ` : leading ? `${token} ${body}` : `${body} ${token}`;
    setInput(next);
    setSkillName(name);
    setSkillOpen(false);
    setSlashAt(null);
    // 等 DOM 重建完再聚焦，否则光标落在旧节点上
    requestAnimationFrame(() => senderRef.current?.focus?.());
  };

  // 切会话时 App 会先清空 items：窗口恢复到默认值
  useEffect(() => {
    if (items.length === 0) setVisibleCount(INITIAL_VISIBLE);
  }, [items.length]);

  // 搜索定位的目标可能在窗口外：把窗口扩到包含它，否则 #msg-xxx 找不到
  useEffect(() => {
    if (!focusId) return;
    const idx = items.findIndex((it) => it.id === focusId);
    if (idx >= 0) setVisibleCount((c) => Math.max(c, items.length - idx));
  }, [focusId, items]);

  // 展开更早消息后保持视口锚定（新内容在上方，scrollTop 顺延同样的高度差）
  useLayoutEffect(() => {
    const el = scrollRef.current;
    const a = anchor.current;
    if (!el || !a) return;
    anchor.current = null;
    el.scrollTop = a.top + (el.scrollHeight - a.height);
  }, [visibleCount]);

  // 记录「能否滚动」：不能滚动时滚不到顶部，自动加载永远不会触发，需要用按钮兜底。
  // 只在条数/窗口变化时测一次——流式期间长度不变，就不必每个 token 都读一次布局
  useLayoutEffect(() => {
    if (hiddenCount <= 0) return;
    const el = scrollRef.current;
    if (!el) return;
    const can = el.scrollHeight > el.clientHeight + 40;
    setScrollable((prev) => (prev === can ? prev : can));
  }, [items.length, visibleCount, hiddenCount]);

  /** 往前加载一批历史消息 */
  const loadEarlier = () => {
    const el = scrollRef.current;
    if (el) anchor.current = { height: el.scrollHeight, top: el.scrollTop };
    setVisibleCount((c) => c + LOAD_STEP);
  };

  /** 滚到顶部附近自动加载（内容还没撑满一屏时不自动，交给按钮） */
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el || hiddenCount <= 0) return;
    if (el.scrollTop > 60) return;
    if (el.scrollHeight <= el.clientHeight + 200) return;
    loadEarlier();
  };

  // 模型子菜单：按服务商分组，组名用服务商展示名；组内条目用模型显示名称
  const modelGroups: { name: string; children: { key: string; label: React.ReactNode }[] }[] = [];
  const groupIndex: Record<string, number> = {};
  models.forEach((m) => {
    const pname = providerNames[m.provider] || m.provider;
    if (groupIndex[m.provider] === undefined) {
      groupIndex[m.provider] = modelGroups.length;
      modelGroups.push({ name: pname, children: [] });
    }
    modelGroups[groupIndex[m.provider]].children.push({
      key: m.id,
      label: <span className="mm-model">{m.name || m.model}</span>,
    });
  });

  // 推理等级显示回退：会话里存的值可能不在当前模型厂家支持的档位里
  // （比如换到只支持 low/high 的模型），此时显示上退到该厂家最高档；
  // 实际请求参数由后端按厂家 spec 决定，不受这里影响。
  const showReasoning: ReasoningLevel =
    reasoningLevels.length === 0
      ? "high"
      : reasoningLevels.includes(reasoning)
        ? reasoning
        : reasoningLevels[reasoningLevels.length - 1];
  // 推理等级仅在智能体模式下可配置（快速对话不思考，直接输出）
  const canReasoning = mode === "agent" && reasoningLevels.length > 0;

  // 模型 / 推理等级菜单：宽屏挂在模型胶囊上，窄屏收进「⋯」，两者共用同一份配置
  const modelMenuSelected = [
    activeModelId ?? "",
    ...(canReasoning ? [`rc:${showReasoning}`] : []),
  ];
  const modelMenuOnClick: MenuProps["onClick"] = ({ key }) => {
    if (key === "__manage__") {
      onManageModels();
      return;
    }
    if (key.startsWith("rc:")) {
      onSetReasoning(key.slice(3) as ReasoningLevel);
      return;
    }
    onActivateModel(key);
  };
  const modelMenuItems: MenuProps["items"] = [
    {
      key: "model",
      label: (
        <span className="mm-row">
          <span className="mm-label">模型</span>
          <span className="mm-value">{modelName || "选择模型"}</span>
        </span>
      ),
      children: [
        ...modelGroups.map((g) => ({
          type: "group" as const,
          label: g.name,
          children: g.children,
        })),
        { type: "divider" as const },
        { key: "__manage__", label: "管理模型", icon: <SettingOutlined /> },
      ],
    },
    // 推理等级行只在智能体模式下出现，档位按当前模型厂家过滤；
    // 快速对话本身就不思考，没有再配置的意义
    ...(canReasoning
      ? [
          {
            key: "reasoning",
            label: (
              <span className="mm-row">
                <span className="mm-label">推理等级</span>
                <span className="mm-value">{LEVEL_LABEL[showReasoning]}</span>
              </span>
            ),
            children: reasoningLevels.map((l) => ({
              key: `rc:${l}`,
              label: LEVEL_LABEL[l],
            })),
          },
        ]
      : []),
  ];

  /** 选中文件后立即上传到工作区 .attachments/，结果暂存 pending 等发送 */
  const onPickFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = ""; // 允许再次选择同一个文件
    if (!f) return;
    setUploading(true);
    try {
      const att = await uploadFile(f, workspaceId);
      setPending((prev) => [...prev, att]);
      message.success(`已附加「${f.name}」`);
    } catch (err) {
      message.error(`上传失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setUploading(false);
    }
  };

  const removePending = (idx: number) => {
    setPending((prev) => prev.filter((_, i) => i !== idx));
  };

  // 贴近底部时跟随滚动；用户向上翻历史时不打扰；新消息插入时强制回底
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 有定位请求时让位给 focusId 的滚动逻辑，避免被「跳到底部」覆盖
    if (focusId) {
      prevLen.current = items.length;
      return;
    }
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 140;
    if (nearBottom || items.length > prevLen.current) {
      el.scrollTop = el.scrollHeight;
    }
    prevLen.current = items.length;
  }, [items, focusId]);

  // 决策卡片（ask_user）出现时消息条数不变，上面的跟随滚动不会触发；
  // 卡片插进最后一条助手消息后会撑高内容、把卡片顶出可视区，这里补一次强制回底。
  useEffect(() => {
    const appeared = !!pendingAsk && !prevAsk.current;
    prevAsk.current = pendingAsk ?? null;
    if (appeared && !focusId) {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
  }, [pendingAsk, focusId]);

  // 搜索命中定位：滚动 + 闪烁由 App 统一处理（见 App 的 focus effect）；
  // 这里只负责在有定位请求时让位，不把视图自动拉到底部。

  const roles: Record<string, any> = useMemo(
    () => ({
      user: {
        placement: "end",
        variant: "filled",
      },
      assistant: {
        placement: "start",
        // 无外框、无背景，文字直接落在白色聊天区上（参考千问）
        variant: "borderless",
      },
      notice: {
        placement: "start",
        variant: "borderless",
      },
    }),
    []
  );

  // 只有真正影响气泡的依赖变化时才重建列表：输入框打字、附件增删等局部状态
  // 不应该把整段历史重新构造一遍（长会话下这是输入卡顿的主要来源之一）
  const bubbleItems = useMemo(() => items.slice(hiddenCount).map((it) => {
    // 每条消息套一层带 id 的容器，供搜索命中后 scrollIntoView 定位
    if (it.role === "assistant") {
      const waiting =
        it.status === "streaming" &&
        !it.content &&
        !it.reasoning &&
        !(it.tools && it.tools.length);
      return {
        key: it.id,
        role: "assistant",
        loading: waiting,
        content: (
          <div id={`msg-${it.id}`}>
            <AssistantBody
              item={it}
              onRegenerate={busy ? undefined : onRegenerate}
              onDelete={busy ? undefined : onDelete}
              onTrajectory={onTrajectory}
              workspaceId={workspaceId}
              pendingAsk={pendingAsk}
              onAnswerAsk={onAnswerAsk}
            />
          </div>
        ),
      };
    }
    if (it.role === "notice") {
      return { key: it.id, role: "notice", content: <span className="notice">{it.content}</span> };
    }
    return {
      key: it.id,
      role: "user",
      content: (
        <span id={`msg-${it.id}`}>
          {it.content}
          <AttachmentPreview attachments={it.attachments} workspaceId={workspaceId} />
          {onEditResend && !busy && (
            <div className="msg-toolbar" style={{ marginTop: 6 }}>
              <Tooltip title="编辑重发">
                <span
                  className="msg-tool"
                  onClick={() => {
                    setInput(it.content);
                    onEditResend(it.id);
                  }}
                >
                  <EditOutlined />
                </span>
              </Tooltip>
              {onDelete && (
                <Tooltip title="删除此条及之后消息">
                  <span className="msg-tool" onClick={() => onDelete(it.id)}>
                    <DeleteOutlined />
                  </span>
                </Tooltip>
              )}
            </div>
          )}
        </span>
      ),
    };
  }), [
    items,
    hiddenCount,
    busy,
    onRegenerate,
    onDelete,
    onTrajectory,
    workspaceId,
    pendingAsk,
    onAnswerAsk,
    onEditResend,
  ]);

  return (
    <>
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        {items.length === 0 ? (
          <WelcomePane mode={mode} onPick={onSend} />
        ) : (
          <div className="chat-inner">
            {hiddenCount > 0 && !scrollable && (
              <div className="load-earlier">
                <Button type="text" size="small" onClick={loadEarlier}>
                  加载更早的 {hiddenCount} 条消息
                </Button>
              </div>
            )}
            <Bubble.List items={bubbleItems} roles={roles} />
          </div>
        )}
      </div>
      <div className="composer">
        <div className="composer-inner">
          <div className="composer-body">
          <div className="composer-top">
            <WorkspaceChip
              workspaces={workspaces}
              workspaceId={workspaceId}
              locked={workspaceLocked}
              onChange={onWorkspaceChange}
            />
            {/* 上下文水位 */}
            {contextLimit != null && contextLimit > 0 && (
              <Tooltip title={`上下文已用 ${contextUsage?.toLocaleString() ?? 0} / ${contextLimit.toLocaleString()} 字符`}>
                <span className={`ctx-meter ${(() => {
                  const pct = (contextUsage ?? 0) / contextLimit;
                  return pct > 0.8 ? "ctx-danger" : pct > 0.5 ? "ctx-warn" : "";
                })()}`}>
                  <span className="ctx-meter-bar" style={{ width: `${Math.min(100, ((contextUsage ?? 0) / contextLimit) * 100)}%` }} />
                  <span className="ctx-meter-text">{Math.round(((contextUsage ?? 0) / contextLimit) * 100)}%</span>
                </span>
              </Tooltip>
            )}
            {/* 待发送附件 chips：发送后清空 */}
            {pending.map((a, i) => (
              <span key={i} className="pending-att">
                {a.mime.startsWith("image/") ? <PictureOutlined /> : <PaperClipOutlined />}
                <span className="pending-att-name">{a.name}</span>
                <span className="pending-att-x" onClick={() => removePending(i)}>×</span>
              </span>
            ))}
            {uploading && <span className="pending-att uploading">上传中…</span>}
          </div>
          {/* 输入框定位容器：审批面板绝对定位覆盖在 Sender 上，塌缩态恰好盖满，
              展开「查看详细」时锚定底部向上撑开 */}
          <div className={`composer-slot${confirm ? " blocked" : ""}`}>
            <Sender
              ref={senderRef}
              value={input}
              onChange={handleInputChange}
              components={{ input: SkillInput }}
              onSubmit={(v: string) => {
                const { skills: picked, text } = parseComposer(v);
                onSend(text.trim(), pending.length ? pending : undefined, picked);
                setInput("");
                setPending([]);
                setSkillName(null);
              }}
              loading={busy}
              onCancel={onStop}
              allowSpeech
              actions={false}
              placeholder={
                mode === "chat"
                  ? "有问题尽管问，我会直接回答（不做推理思考，可开启联网搜索）"
                  : "描述任务，Agent 会调用本地工具完成（agent 模式）"
              }
              footer={({ components }) => {
                const { SendButton, LoadingButton, SpeechButton } = components;
                return (
                  <div className="composer-toolbar">
                    <div className="composer-toolbar-left">
                      {/* 「+」上传菜单：图片 / 文件。
                          菜单项用原生 label htmlFor 直连隐藏 input ——
                          手机浏览器（iOS/安卓）对 antd 菜单里的 JS input.click()
                          可能不视为用户手势导致选图面板不弹出，label 是原生激活无此问题 */}
                      <Dropdown
                        trigger={["click"]}
                        placement="topLeft"
                        menu={{
                          items: [
                            {
                              key: "image",
                              label: (
                                <label className="plus-menu-label" htmlFor="att-input-image">
                                  <PictureOutlined /> 上传图片
                                </label>
                              ),
                            },
                            {
                              key: "file",
                              label: (
                                <label className="plus-menu-label" htmlFor="att-input-file">
                                  <PaperClipOutlined /> 上传文件
                                </label>
                              ),
                            },
                          ],
                        }}
                      >
                        <span className="tb-icon-btn tb-plus" title="上传附件">
                          <PlusOutlined />
                        </span>
                      </Dropdown>
                      {/* 模式选择：快速对话 / 智能体（从顶栏迁移至此） */}
                      <Dropdown
                        trigger={["click"]}
                        placement="topLeft"
                        menu={{
                          selectedKeys: [mode],
                          items: [
                            { key: "chat", icon: <ThunderboltOutlined />, label: "快速对话" },
                            { key: "agent", icon: <RobotOutlined />, label: "智能体" },
                          ],
                          onClick: ({ key }) => onMode(key as Mode),
                        }}
                      >
                        <span
                          className="tb-mode"
                          title={mode === "chat" ? "快速对话" : "智能体"}
                        >
                          {mode === "chat" ? <ThunderboltOutlined /> : <RobotOutlined />}
                          <span className="tb-mode-text">
                            {mode === "chat" ? "快速对话" : "智能体"}
                          </span>
                          <DownOutlined className="tb-mode-caret" />
                        </span>
                      </Dropdown>
                      {/* 技能选择（第三个交互按钮）：内置 / 自定义，单选 */}
                      <SkillPicker
                        skills={skills}
                        value={skillName}
                        open={skillOpen}
                        onOpenChange={(o) => {
                          if (o) ensureSkills();
                          else setSlashAt(null);
                          setSkillOpen(o);
                        }}
                        onPick={applySkill}
                      />
                      {/* 确认模式：仅智能体模式有意义（chat 模式无危险本地工具） */}
                      {mode === "agent" && (
                        <ApprovalChip value={approval} onChange={onApproval} />
                      )}
                    </div>
                    <div className="composer-toolbar-right">
                      {/* 模型胶囊：宽屏直接展示；窄屏由「⋯」承载（显隐由 CSS 控制） */}
                      <span className="model-chip-wrap">
                        <Dropdown
                          trigger={["click"]}
                          placement="topRight"
                          menu={{
                            className: "model-menu",
                            selectable: true,
                            selectedKeys: modelMenuSelected,
                            items: modelMenuItems,
                            onClick: modelMenuOnClick,
                          }}
                        >
                          <span
                            className="model-chip"
                            title={`${modelName || "选择模型"}${
                              canReasoning ? " · " + LEVEL_LABEL[showReasoning] : ""
                            }`}
                          >
                            <span className="model-chip-name">{modelName || "选择模型"}</span>
                            {canReasoning && (
                              <span className="model-chip-level">{LEVEL_LABEL[showReasoning]}</span>
                            )}
                            <UpOutlined className="model-chip-caret" />
                          </span>
                        </Dropdown>
                      </span>
                      {/* 窄屏「⋯」：模型 / 推理等级收在这里，避免工具栏拥挤 */}
                      <Tooltip title="模型与推理等级">
                        <Dropdown
                          trigger={["click"]}
                          placement="topRight"
                          menu={{
                            className: "model-menu",
                            selectable: true,
                            selectedKeys: modelMenuSelected,
                            items: modelMenuItems,
                            onClick: modelMenuOnClick,
                          }}
                        >
                          <span className="tb-icon-btn tb-more">
                            <MoreOutlined />
                          </span>
                        </Dropdown>
                      </Tooltip>
                      {/* 语音输入（浏览器 SpeechRecognition，不支持时自动变禁用态） */}
                      <SpeechButton />
                      {/* 发送按钮保持默认主色圆形样式，在一排文字图标中最醒目 */}
                      {busy ? <LoadingButton /> : <SendButton />}
                    </div>
                  </div>
                );
              }}
            />
          </div>
          <ConfirmPanel info={confirm ?? null} timeout={confirmTimeout} onAnswer={(a) => onAnswerConfirm?.(a)} />
          </div>
          {/* 「+」菜单用的文件选择框：视觉隐藏而非 display:none，
              保证手机浏览器通过 label 激活时能正常弹出选图面板 */}
          <input id="att-input-image" className="visually-hidden-file" type="file" accept="image/*" onChange={onPickFile} />
          <input id="att-input-file" className="visually-hidden-file" type="file" onChange={onPickFile} />
          <div className="hint">Enter 发送 / Shift+Enter 换行 · 本地部署，数据不出本机</div>
        </div>
      </div>
    </>
  );
}
