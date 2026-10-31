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
import { fmtSec } from "../format";
import Markdown from "./Markdown";
import ToolChain from "./ToolChain";
import RunHeader from "./RunHeader";
import WelcomePane from "./WelcomePane";
import SkillInput from "./composer/SkillInput";
import SkillPicker from "./composer/SkillPicker";
import {
  insertSkillToken,
  parseComposer,
  removeAllSkillTokens,
  serializeForSend,
  skillToken,
} from "./composer/skillMarks";
import { detectSlashCommand, skillQuery, stripSkillCommand } from "./composer/slashCommands";
import { matchSkills } from "./composer/skillSearch";
import ConfirmPanel from "./ConfirmPanel";

/** token 数缩写：59300 → 59.3K（与参考图一致） */
function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
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
  onEdit,
  onRegenerate,
  onDelete,
  onTrajectory,
  pinned = false,
  extraClass,
}: {
  item: ChatItem;
  /** 编辑重发（仅最后一条 user 消息提供）；排在复制之前 */
  onEdit?: () => void;
  onRegenerate?: (msgId: string) => void;
  onDelete?: (msgId: string) => void;
  /** 打开本会话的运行轨迹抽屉（P0 事实层） */
  onTrajectory?: () => void;
  /** 是否常驻显示（最后一对问答），否则鼠标悬停该条消息才出现 */
  pinned?: boolean;
  /** 追加到根节点的类名（user 气泡右对齐用 msg-toolbar-user） */
  extraClass?: string;
}) {
  const btnRef = useRef<HTMLSpanElement>(null);
  const hasContent = !!item.content.trim();
  const usage = item.usage;
  const showUsage = !!usage && usage.total_tokens > 0;
  const showSec = item.sec != null;

  if (
    !hasContent && !showUsage && !showSec &&
    !onEdit && !onRegenerate && !onDelete && !onTrajectory
  ) return null;

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
    <div className={`msg-toolbar${extraClass ? ` ${extraClass}` : ""}${pinned ? " msg-toolbar-pinned" : ""}`}>
      {onEdit && (
        <Tooltip title="编辑重发">
          <span className="msg-tool" onClick={onEdit}>
            <EditOutlined />
          </span>
        </Tooltip>
      )}
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
        <Tooltip title="删除">
          <span className="msg-tool" onClick={() => onDelete(item.id)}>
            <DeleteOutlined />
          </span>
        </Tooltip>
      )}
      <span className="msg-toolbar-spacer" />
      {showUsage && (
        <Tooltip
          title={
            `输入 ${fmtTokens(usage.prompt_tokens)} / 输出 ${fmtTokens(usage.completion_tokens)} tok` +
            (usage.cached_tokens ? `（缓存命中 ${fmtTokens(usage.cached_tokens)}）` : "") +
            ` ｜ 模型调用 ${usage.llm_calls} 次 ｜ 工具 ${usage.tool_calls} 次`
          }
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
 *  · record：只读留存 —— 已回答显示所选，停止/断连导致失效时显示「未回答」。
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
          {answered ? src.answer : "（任务已停止或连接已断开，Agent 按未回答继续）"}
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
  pinned = false,
  hideToolbar = false,
}: {
  item: ChatItem;
  onRegenerate?: (msgId: string) => void;
  onDelete?: (msgId: string) => void;
  onTrajectory?: () => void;
  workspaceId: string;
  pendingAsk?: PendingAsk | null;
  onAnswerAsk?: (answer: string) => void;
  /** 最后一对问答：工具栏常驻 */
  pinned?: boolean;
  /** 多选删除的卡片视图：勾选框在卡片行首，气泡内不再显示工具栏 */
  hideToolbar?: boolean;
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
      {item.run ? (
        // 运行头行：计时 + 最后动作固定在正文上方；展开是本轮动作时间线
        <RunHeader
          run={item.run}
          tools={item.tools}
          streaming={item.status === "streaming"}
        />
      ) : item.tools && item.tools.length > 0 ? (
        // 老会话没有 run 记录 → 退回原来的工具折叠块
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
      {!hideToolbar && item.status !== "streaming" ? (
        <MessageToolbar
          item={item}
          onRegenerate={onRegenerate}
          onDelete={onDelete}
          onTrajectory={onTrajectory}
          pinned={pinned}
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
  onSend: (text: string, attachments?: Attachment[]) => void;
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
  /** 编辑重发（仅最后一条 user 消息）：气泡原位变成内联编辑框 */
  onEditResend?: (msgId: string) => void;
  /** 正在内联编辑的消息 id（该气泡渲染为编辑框）；null = 未在编辑 */
  editingId?: string | null;
  /** 内联编辑框「发送」：先删掉该条（最后一次信息）再用新文本重发 */
  onEditSend?: (text: string) => void;
  onCancelEdit?: () => void;
  /** 多选删除：按选中的消息 id 删除（任意组合，不再限定「此条及之后」） */
  onDeleteMessages?: (ids: string[]) => void;
  /** 打开本会话的运行轨迹抽屉（P0 事实层） */
  onTrajectory?: () => void;
  /** 内联提问：正在等待作答的 ask_user 卡片（渲染在当前流式气泡内） */
  pendingAsk?: PendingAsk | null;
  onAnswerAsk?: (answer: string) => void;
  /** 权限审批：正在等待作答的确认请求（覆盖在输入框位置上） */
  confirm?: ConfirmInfo | null;
  onAnswerConfirm?: (approve: boolean) => void;
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
  /** 技能管理页点「在对话框中试用」：nonce 变化时把技能插进输入框 */
  trialSkill?: { name: string; source: "builtin" | "custom"; nonce: number } | null;
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

/** 一轮最多同时选几个技能：system prompt 每次模型调用都会重发，选太多会挤爆上下文 */
const MAX_SKILLS = 3;

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

/**
 * 内联编辑框（编辑重发，参考图3）：原位替换最后一条 user 气泡。
 * 文本为空、或与原文完全相同时「发送」禁用；确认后由 App 先删掉这条再重发。
 */
function InlineEditBox({
  original,
  onCancel,
  onSend,
}: {
  original: string;
  onCancel: () => void;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState(original);
  const ok = !!text.trim() && text.trim() !== original.trim();
  const submit = () => {
    if (ok) onSend(text);
  };
  return (
    <div className="edit-inline">
      <Input.TextArea
        autoFocus
        autoSize={{ minRows: 3, maxRows: 8 }}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="edit-inline-actions">
        <Button size="small" onClick={onCancel}>
          取消
        </Button>
        <Button size="small" type="primary" disabled={!ok} onClick={submit}>
          发送
        </Button>
      </div>
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
  editingId,
  onEditSend,
  onCancelEdit,
  onDeleteMessages,
  onTrajectory,
  contextUsage,
  contextLimit,
  pendingAsk,
  onAnswerAsk,
  confirm,
  onAnswerConfirm,
  models,
  providerNames,
  activeModelId,
  modelName,
  reasoning,
  reasoningLevels,
  onActivateModel,
  onSetReasoning,
  onManageModels,
  trialSkill,
}: Props) {
  const [input, setInput] = useState("");
  const senderRef = useRef<ElementRef<typeof Sender>>(null);
  // 技能选择：清单懒加载（首次点开或输入 "/" 时才请求）
  const [skills, setSkills] = useState<SkillItem[] | null>(null);
  const [skillOpen, setSkillOpen] = useState(false);
  // 触发本次面板的 "/" 在输入串里的位置（选中技能后要把它和查询词一起删掉）
  const [slashAt, setSlashAt] = useState<number | null>(null);
  // "/" 之后、本行的查询词：继续打字会实时缩小候选
  const [skillQueryText, setSkillQueryText] = useState("");
  // 已选技能直接从输入串解析，避免「状态与输入框不同步」这类问题
  const pickedSkills = useMemo(() => parseComposer(input).skills, [input]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevLen = useRef(0);
  // 是否处于「跟随底部」状态：用户手动往上翻历史时置 false，滚回底部自动恢复
  const stick = useRef(true);
  // 「发送后顶到最上面」的锚点消息 id + 它下面的动态留白
  const anchorId = useRef<string | null>(null);
  const tailRef = useRef<HTMLDivElement>(null);
  // 视口模式：pinned = 本轮用户消息钉在顶部、期间绝不移动视口；follow = 贴底跟随。
  // 滚动权只归这一个状态机：Bubble.List 的 autoScroll 已关闭，避免两套逻辑抢滚动条。
  const viewMode = useRef<"pinned" | "follow">("follow");
  // 发送 / 重新生成的调用处置位：下一次布局进入 pinned 并做一次定位
  const pendingPin = useRef(false);
  // 本轮是否已切到「贴底跟随」（回复超一屏）；带滞回，见跟随 effect
  const roundFollow = useRef(false);
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
  // 多选删除（参考图2）：选中态独立于 items；selectMode 期间输入区整体隐藏
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());

  /** 技能清单：每次打开选择器都重新拉，保证管理页的启停立即生效 */
  const ensureSkills = () => {
    fetchSkills()
      .then(setSkills)
      .catch(() => setSkills([]));
  };

  /** 面板当前候选：按查询词过滤并排序；null = 清单还没加载完 */
  const visibleSkills = useMemo(() => {
    if (skills === null) return null;
    return matchSkills(skills.filter((s) => s.enabled), skillQueryText);
  }, [skills, skillQueryText]);

  /** 结束搜索会话：关闭面板，清掉触发位置与查询词 */
  const endSkillSearch = () => {
    setSkillOpen(false);
    setSlashAt(null);
    setSkillQueryText("");
  };

  // ---------------------------------------------------------------- 多选删除

  /** 一轮对话 = 一条 user 气泡 + 其后的助手气泡（图1/图2 的一整块）。
      user 之前已有助手消息（如开场白）时单独成组，只有分项勾选框。 */
  type SelGroup = {
    key: string;
    userId?: string;
    assistantIds: string[];
    noticeId?: string;
  };

  const selGroups = useMemo(() => {
    const out: SelGroup[] = [];
    let cur: SelGroup | null = null;
    for (const it of items.slice(hiddenCount)) {
      if (it.role === "notice") {
        cur = null;
        out.push({ key: it.id, assistantIds: [], noticeId: it.id });
        continue;
      }
      if (it.role === "user") {
        cur = { key: it.id, userId: it.id, assistantIds: [] };
        out.push(cur);
        continue;
      }
      if (!cur) {
        cur = { key: it.id, assistantIds: [] };
        out.push(cur);
      }
      cur.assistantIds.push(it.id);
    }
    return out;
  }, [items, hiddenCount]);

  const itemById = useMemo(() => new Map(items.map((it) => [it.id, it])), [items]);

  const groupIds = (g: SelGroup) => (g.userId ? [g.userId, ...g.assistantIds] : g.assistantIds);

  /** 进入多选删除：预选中被点击那一整轮（user + 助手），避免留下孤儿回复 */
  const enterSelect = (msgId?: string) => {
    if (busy || !onDeleteMessages) return;
    if (editingId) onCancelEdit?.();
    const g = msgId
      ? selGroups.find((x) => x.userId === msgId || x.assistantIds.includes(msgId))
      : undefined;
    setSelected(new Set(g ? groupIds(g) : msgId ? [msgId] : []));
    setSelectMode(true);
  };

  /** 退出多选删除：输入区与工具栏恢复显示 */
  const exitSelect = () => {
    setSelectMode(false);
    setSelected(new Set());
  };

  const toggleSel = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  /** 轮级全选框（与用户气泡对齐）：整轮一起勾选 / 取消；部分选中时点一下补齐 */
  const toggleGroup = (g: SelGroup) => {
    const ids = groupIds(g);
    setSelected((prev) => {
      const all = ids.every((id) => prev.has(id));
      const next = new Set(prev);
      ids.forEach((id) => (all ? next.delete(id) : next.add(id)));
      return next;
    });
  };

  // 只把当前可见窗口里的消息纳入可选集：全选不应勾掉用户还没加载出来看到的历史
  const selectableIds = useMemo(() => selGroups.flatMap(groupIds), [selGroups]);
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(selectableIds));

  /** 执行删除：无论成败都退出选择态恢复输入区（失败时 App 侧回滚并提示） */
  const confirmDelete = () => {
    onDeleteMessages?.([...selected]);
    exitSelect();
  };

  /**
   * 输入变化：
   *  · 命中斜杠指令（行首或空格后的 "/"）→ 弹出选择器，触发符保留；
   *  · 已在搜索会话中 → 继续输入只更新查询词，候选实时缩小；
   *  · 删掉触发符、按 Esc、发送消息 → 结束会话。
   * 查询词没有匹配时只是不渲染面板（skillOpen 仍为 true），删字可立即恢复。
   */
  const handleInputChange = (v: string) => {
    if (slashAt !== null) {
      if (v[slashAt] === "/") {
        setSkillQueryText(skillQuery(v, slashAt));
        setSkillOpen(true);
        setInput(v);
        return;
      }
      endSkillSearch();   // "/" 被删掉，退出搜索会话
    }

    const hit = detectSlashCommand(input, v);
    if (hit && hit.command.id === "skill" && mode === "agent" && !busy) {
      ensureSkills();
      setSkillOpen(true);
      setSlashAt(hit.index);
      setSkillQueryText(skillQuery(v, hit.index));
    } else if (skillOpen) {
      endSkillSearch();
    }
    setInput(v);
  };

  /**
   * 选择一个技能（选择器是单选动作，插完即关闭）。
   *  · 上限 3 个，重复也计数；
   *  · 前端不做重复性判断，去重交给后端运行时处理；
   *  · 输入框为空放最前，已有文字接在后面，已有技能块则紧跟最后一个技能块；
   *  · 由 "/查询词" 打开时，把触发符与查询词一起删掉再插入。
   */
  const selectSkill = (item: SkillItem) => {
    if (pickedSkills.length >= MAX_SKILLS) {
      message.warning(`最多选择 ${MAX_SKILLS} 个技能`);
      return;
    }
    const leading = slashAt === 0;
    const base = slashAt === null ? input : stripSkillCommand(input, slashAt);
    setInput(insertSkillToken(base, skillToken(item.name, item.source), leading));
    endSkillSearch();
    // 等 DOM 重建完再聚焦，否则光标落在旧节点上
    requestAnimationFrame(() => senderRef.current?.focus?.());
  };

  // 切到快速对话：技能不参与，清掉输入框里的技能块（保留正文）
  useEffect(() => {
    if (mode !== "chat") return;
    endSkillSearch();
    if (!parseComposer(input).skills.length) return;
    setInput(removeAllSkillTokens(input));
    message.info("快速对话模式不使用技能，已移除已选技能");
    // 只在模式切换时触发一次；input 用当前值即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  // 从技能管理页点「在对话框中试用」：nonce 变化才算一次新请求，避免重渲染重复插入
  const lastTrialNonce = useRef(0);
  useEffect(() => {
    if (!trialSkill || trialSkill.nonce === lastTrialNonce.current) return;
    lastTrialNonce.current = trialSkill.nonce;
    if (mode !== "agent") return;
    const picked = parseComposer(input).skills;
    if (picked.length >= MAX_SKILLS) {
      message.warning(`最多选择 ${MAX_SKILLS} 个技能`);
      return;
    }
    setInput(
      insertSkillToken(
        input,
        skillToken(trialSkill.name, trialSkill.source),
        picked.length === 0
      )
    );
    requestAnimationFrame(() => senderRef.current?.focus?.());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trialSkill]);

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
    viewMode.current = "follow"; // 翻历史即退出钉顶模式
    setVisibleCount((c) => c + LOAD_STEP);
  };

  /** 是否贴着底部（距底部 < 120px，约等于「贴着输入框上沿」） */
  const atBottom = (el: HTMLDivElement) =>
    el.scrollHeight - el.scrollTop - el.clientHeight < 120;

  /** 滚到顶部附近自动加载（内容还没撑满一屏时不自动，交给按钮） */
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // 用户手动上翻 → 暂停自动跟随；滚回底部附近 → 恢复
    stick.current = atBottom(el);
    if (hiddenCount <= 0) return;
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

  // 跟随滚动：
  //   ① 刚发出消息 → 把它顶到列表顶部（下方整块区域留给回复）；
  //   ② 之后内容继续增长（流式 token / 工具卡片 / 思考块），只要仍贴着底部就跟随，
  //      保证最新文字始终露在输入框上方；
  //   ③ 用户手动往上翻历史时不打扰，滚回底部附近自动恢复（见 onScroll）。
  //
  // 「顶部对齐」为什么需要留白：把列表末尾的消息顶到视口顶部，需要 scrollTop 超过
  // scrollHeight - clientHeight，浏览器会钳到最大值，消息只能停在底部。
  // 所以在锚点下方补一块动态留白，让可滚动范围刚好够把锚点推到顶部。
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 有定位请求时让位给 focusId 的滚动逻辑；搜索跳转属于 follow 模式
    if (focusId) {
      prevLen.current = items.length;
      viewMode.current = "follow";
      roundFollow.current = false;
      return;
    }
    const prev = prevLen.current;
    prevLen.current = items.length;

    // 发送 / 重新生成：锚点就绪后进入「钉顶」模式（调用处已置 pendingPin）
    let wantPin = false;
    if (items.length > prev) {
      // 新插入的尾巴里可能有用户消息（后面常紧跟助手占位气泡），
      // 以「本次新增的最后一条用户消息」为锚点。
      for (let i = items.length - 1; i >= prev; i--) {
        if (items[i].role === "user" && i >= items.length - 2) {
          anchorId.current = items[i].id;
          break;
        }
      }
    }
    if (pendingPin.current) {
      // 编辑重发是「先截断、再插入」两段式变更：锚点还没进 DOM 时等下一次提交，
      // 避免把 pendingPin 消费在空锚点上导致该轮失去钉顶。
      const probe = anchorId.current
        ? el.querySelector<HTMLElement>(`#msg-${anchorId.current}`)
        : null;
      if (probe) {
        pendingPin.current = false;
        viewMode.current = "pinned";
        stick.current = true;
        wantPin = true;
      }
    }

    const tail = tailRef.current;
    if (viewMode.current !== "pinned") {
      // follow 模式：不留白，按 stick 贴底跟随
      anchorId.current = null;
      roundFollow.current = false;
      if (tail) tail.style.height = "0px";
      if (stick.current) el.scrollTop = el.scrollHeight;
      return;
    }

    const anchorEl = anchorId.current
      ? el.querySelector<HTMLElement>(`#msg-${anchorId.current}`)
      : null;
    if (!anchorEl) {
      // 锚点已不在列表里（切会话 / 截断）：回退 follow
      anchorId.current = null;
      viewMode.current = "follow";
      roundFollow.current = false;
      if (tail) tail.style.height = "0px";
      if (stick.current) el.scrollTop = el.scrollHeight;
      return;
    }

    // 留白高度 = 恰好让锚点能顶到视口顶部所需的额外空间（内容本身够高时为 0）
    // 定位基准取气泡根节点（#msg 在气泡内容内部，与可视气泡顶有固定偏移），
    // 留白与滚动目标统一按「气泡顶距视口顶 5px」计算，保证目标不被 scrollHeight 钳制
    const spacer = tail ? tail.offsetHeight : 0;
    const cRect = el.getBoundingClientRect();
    const bubbleEl = anchorEl.closest<HTMLElement>(".ant-bubble") ?? anchorEl;
    const anchorTop = bubbleEl.getBoundingClientRect().top - cRect.top + el.scrollTop;
    const contentBelow = el.scrollHeight - spacer - anchorTop;
    const pad = Math.min(el.clientHeight, Math.max(0, el.clientHeight - contentBelow - 5));
    if (tail) tail.style.height = `${pad}px`;

    // 用户手动翻走时不动视口（发送那一刻除外）
    if (!stick.current && !wantPin) return;

    // 钉顶视图下「本轮内容超出一屏」的量：运行时间线卡片展开/收起会让它短暂波动，
    // 所以用带滞回的双阈值：超出 48px 才切贴底跟随；收回不超屏才回到钉顶。
    const over = contentBelow - (el.clientHeight - 8);
    if (wantPin) roundFollow.current = false;
    if (!roundFollow.current && over > 48) roundFollow.current = true;
    else if (roundFollow.current && over < 0) roundFollow.current = false;

    if (roundFollow.current) {
      // 回复确实长到一屏装不下：贴底跟随，保证最新文字可见
      el.scrollTop = el.scrollHeight - el.clientHeight;
    } else {
      // 装得下（含回合结束时间线收起后）：用户消息钉在顶部，期间视口不再移动
      el.scrollTop = Math.min(anchorTop - 5, el.scrollHeight - el.clientHeight);
    }
  }, [items, focusId]);

  // 决策卡片（ask_user）出现时消息条数不变，上面的跟随滚动不会触发；
  // 卡片插进最后一条助手消息后会撑高内容、把卡片顶出可视区，这里补一次强制回底。
  useEffect(() => {
    const appeared = !!pendingAsk && !prevAsk.current;
    prevAsk.current = pendingAsk ?? null;
    if (appeared && !focusId && viewMode.current === "follow") {
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
      // 内联编辑框（编辑重发）：贴左侧整行展开（参考图3），无气泡底色
      editor: {
        placement: "start",
        variant: "borderless",
      },
    }),
    []
  );

  // 只有真正影响气泡的依赖变化时才重建列表：输入框打字、附件增删等局部状态
  // 不应该把整段历史重新构造一遍（长会话下这是输入卡顿的主要来源之一）
  const bubbleItems = useMemo(() => {
    // 最后一对问答：工具栏常驻（其中最后一条 user / assistant 各算一个）
    let lastUserId = "";
    let lastAssistantId = "";
    for (let i = items.length - 1; i >= 0; i--) {
      if (!lastAssistantId && items[i].role === "assistant") lastAssistantId = items[i].id;
      if (!lastUserId && items[i].role === "user") lastUserId = items[i].id;
      if (lastUserId && lastAssistantId) break;
    }
    return items.slice(hiddenCount).map((it) => {
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
              onRegenerate={
                busy
                  ? undefined
                  : (id: string) => {
                      pendingPin.current = true; // 重新生成同样钉到该轮顶部
                      onRegenerate?.(id);
                    }
              }
              onDelete={busy ? undefined : (id: string) => enterSelect(id)}
              onTrajectory={onTrajectory}
              workspaceId={workspaceId}
              pendingAsk={pendingAsk}
              onAnswerAsk={onAnswerAsk}
              pinned={it.id === lastAssistantId}
            />
          </div>
        ),
      };
    }
    if (it.role === "notice") {
      return { key: it.id, role: "notice", content: <span className="notice">{it.content}</span> };
    }
    // 编辑重发中：气泡原位换成内联输入框（参考图3）；确认后由 App 删旧发新
    if (editingId === it.id) {
      return {
        key: it.id,
        role: "editor",
        content: (
          <div id={`msg-${it.id}`}>
            <InlineEditBox
              original={it.content}
              onCancel={() => onCancelEdit?.()}
              onSend={(t) => {
                pendingPin.current = true; // 重发后把新消息钉到列表顶部
                onEditSend?.(t);
              }}
            />
          </div>
        ),
      };
    }
    return {
      key: it.id,
      role: "user",
      content: (
        <span id={`msg-${it.id}`}>
          {it.content}
          <AttachmentPreview attachments={it.attachments} workspaceId={workspaceId} />
        </span>
      ),
      // 小工具栏放气泡下方（Bubble 的 footer），而不是塞进蓝色气泡里：
      // 顺序 = 编辑重发 / 复制 / 删除（需求图1）；仅最后一条 user 气泡有编辑。
      footer: busy ? undefined : (
        <MessageToolbar
          item={it}
          extraClass="msg-toolbar-user"
          pinned={it.id === lastUserId}
          onEdit={
            it.id === lastUserId && onEditResend ? () => onEditResend(it.id) : undefined
          }
          onDelete={onDeleteMessages ? (id: string) => enterSelect(id) : undefined}
        />
      ),
    };
    });
  }, [
    items,
    hiddenCount,
    busy,
    onRegenerate,
    onTrajectory,
    workspaceId,
    pendingAsk,
    onAnswerAsk,
    onEditResend,
    editingId,
    onEditSend,
    onCancelEdit,
    onDeleteMessages,
  ]);

  return (
    <>
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        {items.length === 0 ? (
          <WelcomePane
            mode={mode}
            onPick={(t: string) => {
              pendingPin.current = true;
              onSend(t);
            }}
          />
        ) : (
          <div className="chat-inner">
            {hiddenCount > 0 && !scrollable && (
              <div className="load-earlier">
                <Button type="text" size="small" onClick={loadEarlier}>
                  加载更早的 {hiddenCount} 条消息
                </Button>
              </div>
            )}
            {/* 多选删除（图1）：每轮对话一张卡片，行首双勾选框（轮级 + 分项） */}
            {selectMode ? (
              <div className="select-cards">
                {selGroups.map((g) => {
                  if (g.noticeId) {
                    const it = itemById.get(g.noticeId);
                    return it ? (
                      <div key={g.key} className="notice">
                        {it.content}
                      </div>
                    ) : null;
                  }
                  const user = g.userId ? itemById.get(g.userId) : undefined;
                  const ids = groupIds(g);
                  const checkedCount = ids.filter((id) => selected.has(id)).length;
                  const all = ids.length > 0 && checkedCount === ids.length;
                  return (
                    <div className="turn-card" key={g.key}>
                      {user && (
                        <div className="turn-row turn-row-user">
                          <Checkbox
                            className="turn-check"
                            checked={all}
                            indeterminate={checkedCount > 0 && !all}
                            onChange={() => toggleGroup(g)}
                          />
                          <div className="turn-user" id={`msg-${user.id}`}>
                            {user.content}
                            <AttachmentPreview
                              attachments={user.attachments}
                              workspaceId={workspaceId}
                            />
                          </div>
                        </div>
                      )}
                      {g.assistantIds.map((id) => {
                        const it = itemById.get(id);
                        if (!it) return null;
                        return (
                          <div className="turn-row turn-row-assistant" key={id}>
                            <Checkbox
                              className="turn-check"
                              checked={selected.has(id)}
                              onChange={() => toggleSel(id)}
                            />
                            <div className="turn-assistant" id={`msg-${id}`}>
                              <AssistantBody
                                item={it}
                                onTrajectory={onTrajectory}
                                workspaceId={workspaceId}
                                hideToolbar
                              />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  );
                })}
              </div>
            ) : (
              /* autoScroll 必须关：它会在「距底部很近」时自动 scrollTo 底部，
                 而钉顶视图恰好距底部 8px，会被它误判并在回合结束时抢滚动条 */
              <Bubble.List autoScroll={false} items={bubbleItems} roles={roles} />
            )}
            {/* 「发送后顶到最上面」所需的动态留白，高度由跟随滚动 effect 直接写入 */}
            <div className="chat-tail-space" ref={tailRef} aria-hidden="true" />
          </div>
        )}
      </div>
      {selectMode ? (
        /* 多选删除（图2）：底部 全选 / 取消 / 删除；期间输入框与工具栏一律隐藏 */
        <div className="select-bar">
          <Checkbox
            className="select-all"
            checked={allSelected}
            indeterminate={!allSelected && selected.size > 0}
            onChange={toggleAll}
          >
            全选
          </Checkbox>
          <div className="select-actions">
            <Button type="text" onClick={exitSelect}>
              取消
            </Button>
            <Button
              type="primary"
              className="select-del"
              disabled={!selected.size}
              onClick={confirmDelete}
            >
              删除
            </Button>
          </div>
        </div>
      ) : (
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
                // 输入框发送 = 放弃内联编辑（编辑只从内联框确认发送），防止误截断
                if (editingId) onCancelEdit?.();
                // 技能块序列化成「裸名字 + 不可见包裹字符」，由后端运行时自行查找
                const message = serializeForSend(v).trim();
                if (!message && !pending.length) return;
                pendingPin.current = true; // 发送后把这条消息钉到列表顶部
                onSend(message, pending.length ? pending : undefined);
                setInput("");
                setPending([]);
                endSkillSearch();
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
                      {/* 技能选择（第三个交互按钮）：树形列表，插完即关闭；快速对话模式不提供 */}
                      {mode === "agent" && (
                        <SkillPicker
                          skills={visibleSkills}
                          open={skillOpen && (visibleSkills === null || visibleSkills.length > 0)}
                          searching={skillQueryText !== ""}
                          onOpenChange={(o) => {
                            if (o) {
                              // 从工具栏按钮打开：清掉可能残留的 "/查询" 会话，按「浏览全部」展示
                              endSkillSearch();
                              ensureSkills();
                              setSkillOpen(true);
                            } else {
                              endSkillSearch();
                            }
                          }}
                          onSelect={selectSkill}
                        />
                      )}
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
          <ConfirmPanel info={confirm ?? null} onAnswer={(a) => onAnswerConfirm?.(a)} />
          </div>
          {/* 「+」菜单用的文件选择框：视觉隐藏而非 display:none，
              保证手机浏览器通过 label 激活时能正常弹出选图面板 */}
          <input id="att-input-image" className="visually-hidden-file" type="file" accept="image/*" onChange={onPickFile} />
          <input id="att-input-file" className="visually-hidden-file" type="file" onChange={onPickFile} />
          <div className="hint">Enter 发送 / Shift+Enter 换行 · 本地部署，数据不出本机</div>
        </div>
      </div>
      )}
    </>
  );
}
