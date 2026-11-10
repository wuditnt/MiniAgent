/**
 * 任务级 AI 助手面板。
 *
 * 职责边界（与方案 §8 一致）：
 *  · 只把自然语言翻译成结构化 patch，不直接写库
 *  · patch 以字段级 diff 展示，用户点「应用修改」才写入表单草稿
 *  · 真正的保存仍由编辑器的「保存」按钮触发 PUT
 *  · 对话历史由后端持久化（每任务保留 40 条），可一键清空
 */
import { useEffect, useState } from "react";
import { ClearOutlined, SendOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Input, Spin, Tag, message } from "antd";
import type {
  AutomationAssistResult,
  AutomationChatMessage,
  AutomationDraft,
  AutomationPatch,
  AutomationTask,
} from "../types";
import { assistAutomation, clearAutomationChat, fetchAutomationChat } from "../api";
import { describeSchedule, fmtDateTime, fmtRelative } from "./automationTemplates";

interface Props {
  task: AutomationTask;
  draft: AutomationDraft;
  onApply: (patch: AutomationPatch) => void;
  onClose: () => void;
}

const FIELD_LABELS: Record<keyof AutomationPatch, string> = {
  name: "名称",
  prompt: "执行内容",
  workspace_id: "工作区",
  schedule: "执行规则",
  web: "允许联网",
  model_id: "模型",
  enabled: "启用",
};

const EXAMPLES = [
  "改成每周三下午三点执行",
  "开启联网，并把内容改成只关注 OpenAI 和 Anthropic",
  "每隔 2 小时执行一次",
];

function displayValue(field: keyof AutomationPatch, value: unknown): string {
  if (field === "schedule" && value && typeof value === "object") {
    return describeSchedule(value as AutomationDraft["schedule"]);
  }
  if (field === "web" || field === "enabled") return value ? "是" : "否";
  if (field === "prompt") return String(value).slice(0, 120) + (String(value).length > 120 ? "…" : "");
  return value === "" || value === undefined || value === null ? "（跟随全局默认）" : String(value);
}

export default function AutomationAssistant({ task, draft, onApply, onClose }: Props) {
  const [messages, setMessages] = useState<AutomationChatMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<AutomationAssistResult | null>(null);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchAutomationChat(task.id)
      .then((list) => {
        if (alive) setMessages(list);
      })
      .catch((e) => {
        if (alive) setError((e as Error).message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [task.id]);

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setError("");
    setResult(null);
    setApplied(false);
    setMessages((list) => [
      ...list,
      { id: Date.now(), role: "user", content: text, created_at: Date.now() / 1000 },
    ]);
    setSending(true);
    try {
      const res = await assistAutomation(task.id, text, draft);
      setResult(res);
      setMessages((list) => [
        ...list,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: res.reply || "（无回复）",
          patch: res.patch,
          created_at: res.server_now,
        },
      ]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const apply = () => {
    if (!result || !result.patch || Object.keys(result.patch).length === 0) return;
    onApply(result.patch);
    setApplied(true);
  };

  const clear = async () => {
    try {
      await clearAutomationChat(task.id);
      setMessages([]);
      setResult(null);
      setError("");
      message.success("对话历史已清空");
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const patchEntries = result
    ? (Object.keys(result.patch) as (keyof AutomationPatch)[]).filter(
        (k) => result.patch[k] !== undefined
      )
    : [];

  return (
    <aside className="automation-assistant">
      <div className="assistant-head">
        <b>AI 助手</b>
        <span>
          <Button size="small" type="text" icon={<ClearOutlined />} onClick={clear}>
            清空对话
          </Button>
          <Button size="small" type="text" onClick={onClose}>
            收起
          </Button>
        </span>
      </div>

      <div className="assistant-messages">
        {loading ? (
          <div className="assistant-loading">
            <Spin size="small" />
          </div>
        ) : messages.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="用一句话描述你想怎么改，我来生成修改预览"
          >
            <div className="assistant-examples">
              {EXAMPLES.map((ex) => (
                <button key={ex} type="button" onClick={() => setInput(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </Empty>
        ) : (
          messages.map((m) => (
            <div key={m.id} className={`assistant-msg ${m.role}`}>
              <div className="assistant-msg-text">{m.content}</div>
              {m.patch && Object.keys(m.patch).length > 0 && (
                <div className="assistant-msg-patch">
                  {Object.keys(m.patch)
                    .filter((k) => (m.patch as AutomationPatch)[k as keyof AutomationPatch] !== undefined)
                    .map((k) => (
                      <Tag key={k}>{FIELD_LABELS[k as keyof AutomationPatch] || k}</Tag>
                    ))}
                </div>
              )}
            </div>
          ))
        )}
        {sending && (
          <div className="assistant-msg assistant">
            <div className="assistant-msg-text">
              <Spin size="small" /> 正在生成修改建议…
            </div>
          </div>
        )}
      </div>

      {error && <Alert type="error" showIcon message={error} className="assistant-alert" />}

      {result && (
        <div className="assistant-result">
          {result.warnings.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="部分修改被忽略"
              description={
                <ul>
                  {result.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              }
            />
          )}
          {result.questions.length > 0 && (
            <div className="assistant-questions">
              {result.questions.map((q) => (
                <div key={q}>· {q}</div>
              ))}
            </div>
          )}

          <div className="assistant-diff">
            <div className="assistant-diff-title">修改预览</div>
            {patchEntries.length === 0 ? (
              <div className="assistant-diff-empty">本次仅回答，没有配置修改</div>
            ) : (
              patchEntries.map((field) => (
                <div className="assistant-diff-row" key={field}>
                  <span className="assistant-diff-field">{FIELD_LABELS[field]}</span>
                  <span className="assistant-diff-old">{displayValue(field, draft[field])}</span>
                  <span className="assistant-diff-arrow">→</span>
                  <span className="assistant-diff-new">
                    {displayValue(field, result.patch[field])}
                  </span>
                </div>
              ))
            )}
          </div>

          {result.preview.length > 0 && (
            <div className="assistant-preview">
              接下来：{result.preview.map((ts) => fmtDateTime(ts)).join("、")}
            </div>
          )}

          {patchEntries.length > 0 && (
            <Button
              type="primary"
              size="small"
              disabled={applied}
              onClick={apply}
              className="assistant-apply"
            >
              {applied ? "已应用到表单" : "应用修改"}
            </Button>
          )}
          {applied && (
            <div className="assistant-applied-tip">
              修改已写入表单，点击编辑器右下角「保存」后才会真正生效。
            </div>
          )}
        </div>
      )}

      <div className="assistant-input">
        <Input.TextArea
          value={input}
          autoSize={{ minRows: 2, maxRows: 4 }}
          placeholder="例如：改成每周三下午三点执行"
          onChange={(e) => setInput(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="assistant-input-actions">
          <span className="assistant-next">
            下次：{task.next_run_at ? fmtRelative(task.next_run_at) : "未排期"}
          </span>
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={sending}
            disabled={!input.trim()}
            onClick={send}
          >
            发送
          </Button>
        </div>
      </div>
    </aside>
  );
}
