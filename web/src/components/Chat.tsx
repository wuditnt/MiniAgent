/**
 * 消息区 + 输入区：
 *  · assistant 气泡 = 思考过程（可折叠）+ Markdown 正文 + ThoughtChain 工具轨迹 + 状态脚注
 *  · 流式效果来自 items 增量更新，不靠打字机动画
 *  · Sender 的 loading 态自动把发送按钮变成「停止」
 */
import { useEffect, useRef, useState } from "react";
import {
  CheckOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DownOutlined,
  FolderOpenOutlined,
  RobotOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Bubble, Sender } from "@ant-design/x";
import { Collapse, Dropdown, Tag, Tooltip } from "antd";
import type { ChatItem, Mode, WorkspaceItem } from "../types";
import Markdown from "./Markdown";
import ToolChain from "./ToolChain";
import WelcomePane from "./WelcomePane";

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

/**
 * 每轮回答下方的小工具栏：复制文本 / 用量统计 / 耗时。
 * 只有拿到数据的项才显示 —— 从磁盘回放的历史消息没有用量与耗时，不占位。
 *
 * 复制反馈走命令式 DOM（data-state + CSS），不依赖 React 重渲染：
 * 这类「一次性提示」用 DOM 属性最稳，也不会被父级 memo 影响。
 */
function MessageToolbar({ item }: { item: ChatItem }) {
  const btnRef = useRef<HTMLSpanElement>(null);
  const hasContent = !!item.content.trim();
  const usage = item.usage;
  const showUsage = !!usage && usage.total_tokens > 0;
  const showSec = item.sec != null;

  if (!hasContent && !showUsage && !showSec) return null;

  const mark = (ok: boolean) => {
    const el = btnRef.current;
    if (!el) return;
    el.dataset.state = ok ? "ok" : "fail";
    el.dataset.copies = String(Number(el.dataset.copies || 0) + 1);
    setTimeout(() => {
      if (el.dataset.state) delete el.dataset.state;
    }, 1500);
  };

  const copy = async () => {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        // 加超时竞速：某些环境下写剪贴板既不 resolve 也不 reject，会把 await 挂死
        ok = await Promise.race([
          navigator.clipboard.writeText(item.content).then(() => true),
          new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 400)),
        ]);
      }
    } catch {
      ok = false;     // 没权限 / 没有用户手势 → 走降级
    }
    if (!ok) ok = fallbackCopy(item.content);
    mark(ok);
  };

  return (
    <div className="msg-toolbar">
      {hasContent && (
        <Tooltip title="复制回答">
          <span ref={btnRef} className="msg-tool" onClick={copy}>
            <CopyOutlined className="icon-copy" />
            <CheckOutlined className="icon-check" />
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

function AssistantBody({ item }: { item: ChatItem }) {
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
      {item.content ? <Markdown text={item.content} /> : null}
      {item.status === "error" ? (
        <div className="status-error">出错了：{item.error || "未知错误"}</div>
      ) : null}
      {item.status === "cancelled" ? (
        <Tag color="orange" style={{ marginTop: 6 }}>
          已停止{item.reason ? `：${item.reason}` : ""}
        </Tag>
      ) : null}
      {item.status !== "streaming" ? <MessageToolbar item={item} /> : null}
    </div>
  );
}

interface Props {
  items: ChatItem[];
  busy: boolean;
  mode: Mode;
  onSend: (text: string) => void;
  onStop: () => void;
  /** 搜索命中后要定位闪烁的消息 id（形如 h-3）；为 null 表示无定位请求 */
  focusId?: string | null;
  /** 工作区选择：新对话可切换，已开始的对话锁定为只读 */
  workspaces: WorkspaceItem[];
  workspaceId: string;
  onWorkspaceChange: (id: string) => void;
  workspaceLocked: boolean;
}

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

export default function Chat({
  items,
  busy,
  mode,
  onSend,
  onStop,
  focusId,
  workspaces,
  workspaceId,
  onWorkspaceChange,
  workspaceLocked,
}: Props) {
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevLen = useRef(0);

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

  // 搜索命中定位：滚动 + 闪烁由 App 统一处理（见 App 的 focus effect）；
  // 这里只负责在有定位请求时让位，不把视图自动拉到底部。

  const roles: Record<string, any> = {
    user: {
      placement: "end",
      variant: "filled",
      avatar: { icon: <UserOutlined />, style: { background: "#1677ff" } },
    },
    assistant: {
      placement: "start",
      variant: "outlined",
      avatar: { icon: <RobotOutlined />, style: { background: "#e6f4ff", color: "#1677ff" } },
    },
    notice: {
      placement: "start",
      variant: "borderless",
      avatar: {
        icon: <RobotOutlined style={{ color: "#c0c0c0" }} />,
        style: { background: "transparent" },
      },
    },
  };

  const bubbleItems = items.map((it) => {
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
            <AssistantBody item={it} />
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
      content: <span id={`msg-${it.id}`}>{it.content}</span>,
    };
  });

  return (
    <>
      <div className="chat-scroll" ref={scrollRef}>
        {items.length === 0 ? (
          <WelcomePane mode={mode} onPick={onSend} />
        ) : (
          <div className="chat-inner">
            <Bubble.List items={bubbleItems} roles={roles} />
          </div>
        )}
      </div>
      <div className="composer">
        <div className="composer-inner">
          <div className="composer-top">
            <WorkspaceChip
              workspaces={workspaces}
              workspaceId={workspaceId}
              locked={workspaceLocked}
              onChange={onWorkspaceChange}
            />
          </div>
          <Sender
            value={input}
            onChange={setInput}
            onSubmit={(v: string) => {
              onSend(v);
              setInput("");
            }}
            loading={busy}
            onCancel={onStop}
            placeholder={
              mode === "chat"
                ? "有问题尽管问（chat 模式，打开右上角开关可联网）"
                : "描述任务，Agent 会调用本地工具完成（agent 模式）"
            }
          />
          <div className="hint">Enter 发送 / Shift+Enter 换行 · 本地部署，数据不出本机</div>
        </div>
      </div>
    </>
  );
}
