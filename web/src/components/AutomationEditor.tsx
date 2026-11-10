/**
 * 自动化任务编辑器：创建 / 编辑共用。
 *
 * · 左侧表单：名称、执行内容、工作区、模型、联网、启停、重复方式与时间
 * · 底部实时预览接下来 3 次执行时间（本机时区）
 * · 编辑已保存任务时可打开右侧 AI 助手：自然语言 → patch → diff 预览 → 应用到表单
 * · AI 与手动编辑共用后端同一套校验；点「保存」才真正落库
 */
import { useEffect, useMemo, useState } from "react";
import { RobotOutlined, SaveOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Modal,
  Select,
  Switch,
  message,
} from "antd";
import type {
  AutomationDraft,
  AutomationPatch,
  AutomationSchedule,
  AutomationScheduleKind,
  AutomationTask,
  ModelProvider,
  WorkspaceItem,
} from "../types";
import { createAutomation, updateAutomation } from "../api";
import AutomationAssistant from "./AutomationAssistant";
import {
  MIN_INTERVAL_MINUTES,
  WEEKDAY_NAMES,
  defaultSchedule,
  fmtDateTime,
  fromDatetimeInput,
  nextRuns,
  toDatetimeInput,
} from "./automationTemplates";

interface Props {
  open: boolean;
  task: AutomationTask | null;
  /** 模板预填（仅新建时有效） */
  initial: Record<string, unknown> | null;
  workspaces: WorkspaceItem[];
  models: ModelProvider[];
  remote?: boolean;
  onClose: () => void;
  onSaved: (task: AutomationTask) => void;
}

const KIND_OPTIONS: { label: string; value: AutomationScheduleKind }[] = [
  { label: "单次", value: "once" },
  { label: "每天", value: "daily" },
  { label: "每周", value: "weekly" },
  { label: "固定间隔", value: "interval" },
];

function buildDraft(
  task: AutomationTask | null,
  initial: Record<string, unknown> | null,
  fallbackWorkspace: string
): AutomationDraft {
  if (task) {
    return {
      name: task.name,
      prompt: task.prompt,
      workspace_id: task.workspace_id,
      schedule: task.schedule,
      web: task.web,
      model_id: task.model_id,
      enabled: task.enabled,
    };
  }
  return {
    name: String(initial?.name || ""),
    prompt: String(initial?.prompt || ""),
    workspace_id: String(initial?.workspace_id || fallbackWorkspace),
    schedule: (initial?.schedule as AutomationSchedule) || defaultSchedule("daily"),
    web: !!initial?.web,
    model_id: "",
    enabled: true,
  };
}

function validate(draft: AutomationDraft): string {
  if (!draft.name.trim()) return "请填写任务名称";
  if (!draft.prompt.trim()) return "请填写执行内容";
  if (!draft.workspace_id) return "请选择工作区";
  if (draft.schedule.kind === "once" && draft.schedule.at * 1000 <= Date.now()) {
    return "单次执行时间必须晚于当前时间";
  }
  if (draft.schedule.kind === "weekly" && draft.schedule.weekdays.length === 0) {
    return "请至少选择一个星期";
  }
  if (draft.schedule.kind === "interval" && draft.schedule.every_minutes < MIN_INTERVAL_MINUTES) {
    return `间隔不能小于 ${MIN_INTERVAL_MINUTES} 分钟`;
  }
  return "";
}

export default function AutomationEditor({
  open,
  task,
  initial,
  workspaces,
  models,
  remote,
  onClose,
  onSaved,
}: Props) {
  const fallbackWorkspace = workspaces[0]?.id || "default";
  const [draft, setDraft] = useState<AutomationDraft>(() =>
    buildDraft(task, initial, fallbackWorkspace)
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [assistantOpen, setAssistantOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDraft(buildDraft(task, initial, fallbackWorkspace));
    setError("");
    setSaving(false);
    setAssistantOpen(false);
  }, [open, task, initial, fallbackWorkspace]);

  const preview = useMemo(() => nextRuns(draft.schedule, 3), [draft.schedule]);

  const setSchedule = (schedule: AutomationSchedule) =>
    setDraft((d) => ({ ...d, schedule }));

  const switchKind = (kind: AutomationScheduleKind) => {
    if (kind === draft.schedule.kind) return;
    setSchedule(defaultSchedule(kind));
  };

  /** AI 助手给出的 patch → 表单草稿（不落库，仍需用户点保存） */
  const applyPatch = (patch: AutomationPatch) => {
    setDraft((d) => {
      const next: AutomationDraft = { ...d };
      if (patch.name !== undefined) next.name = patch.name;
      if (patch.prompt !== undefined) next.prompt = patch.prompt;
      if (patch.workspace_id !== undefined) next.workspace_id = patch.workspace_id;
      if (patch.schedule !== undefined) next.schedule = patch.schedule;
      if (patch.web !== undefined) next.web = patch.web;
      if (patch.model_id !== undefined) next.model_id = patch.model_id;
      if (patch.enabled !== undefined) next.enabled = patch.enabled;
      return next;
    });
    message.success("已应用到表单，点击「保存」后生效");
  };

  const handleSave = async () => {
    const clean: AutomationDraft = {
      ...draft,
      name: draft.name.trim(),
      prompt: draft.prompt.trim(),
    };
    const problem = validate(clean);
    if (problem) {
      setError(problem);
      return;
    }
    setError("");
    setSaving(true);
    try {
      const saved = task
        ? await updateAutomation(task.id, clean)
        : await createAutomation(clean);
      message.success(task ? "已保存，从下一轮执行开始生效" : "任务已创建");
      onSaved(saved);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const workspaceLocked = !!task?.session_id;
  const modelOptions = [
    { label: "跟随全局默认", value: "" },
    ...(models || []).map((p) => ({
      label: `${p.name}（${p.model || p.provider}）${p.is_active ? " · 当前" : ""}`,
      value: p.id,
    })),
  ];

  return (
    <Modal
      open={open}
      title={task ? "编辑自动化任务" : "新建自动化任务"}
      width={assistantOpen ? 1040 : 720}
      onCancel={onClose}
      maskClosable={false}
      destroyOnClose
      footer={
        <div className="automation-editor-footer">
          {task && !remote && (
            <Button
              icon={<RobotOutlined />}
              type={assistantOpen ? "default" : "dashed"}
              onClick={() => setAssistantOpen((v) => !v)}
            >
              {assistantOpen ? "收起 AI 助手" : "AI 助手"}
            </Button>
          )}
          <span className="automation-editor-footer-right">
            <Button onClick={onClose}>取消</Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={saving}
              disabled={!!remote}
              onClick={handleSave}
            >
              保存
            </Button>
          </span>
        </div>
      }
    >
      <div className="automation-editor-body">
        <div className="automation-form">
          {error && <Alert type="error" showIcon message={error} className="automation-form-alert" />}

          <label className="automation-field">
            <span className="automation-field-label">任务名称</span>
            <Input
              value={draft.name}
              maxLength={60}
              placeholder="例如：每日 AI 新闻简报"
              onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            />
          </label>

          <label className="automation-field">
            <span className="automation-field-label">执行内容</span>
            <Input.TextArea
              value={draft.prompt}
              rows={6}
              placeholder="到点后发给智能体的完整指令：要做什么、输出什么格式、注意什么"
              onChange={(e) => setDraft((d) => ({ ...d, prompt: e.target.value }))}
            />
          </label>

          <div className="automation-field-row">
            <label className="automation-field">
              <span className="automation-field-label">工作区</span>
              <Select
                value={draft.workspace_id}
                disabled={workspaceLocked}
                options={workspaces.map((w) => ({ label: w.name || w.path, value: w.id }))}
                onChange={(v) => setDraft((d) => ({ ...d, workspace_id: v }))}
              />
              {workspaceLocked && (
                <span className="automation-field-hint">已经生成过会话，工作区不可再修改</span>
              )}
            </label>
            <label className="automation-field">
              <span className="automation-field-label">模型</span>
              <Select
                value={draft.model_id}
                options={modelOptions}
                onChange={(v) => setDraft((d) => ({ ...d, model_id: v }))}
              />
            </label>
          </div>

          <div className="automation-switch-row">
            <span>
              <b>允许联网</b>
              <span className="automation-field-hint">开启后可使用联网搜索与网页抓取</span>
            </span>
            <Switch
              checked={draft.web}
              onChange={(v) => setDraft((d) => ({ ...d, web: v }))}
            />
          </div>
          {task && (
            <div className="automation-switch-row">
              <span>
                <b>启用任务</b>
                <span className="automation-field-hint">停用后不再按排期执行，可随时重新启用</span>
              </span>
              <Switch
                checked={draft.enabled}
                onChange={(v) => setDraft((d) => ({ ...d, enabled: v }))}
              />
            </div>
          )}

          <div className="automation-field">
            <span className="automation-field-label">重复方式</span>
            <div className="automation-kind-group">
              {KIND_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  className={`automation-kind ${draft.schedule.kind === opt.value ? "active" : ""}`}
                  onClick={() => switchKind(opt.value)}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {draft.schedule.kind === "once" && (
            <label className="automation-field">
              <span className="automation-field-label">执行时间（本机时区）</span>
              <input
                type="datetime-local"
                className="automation-native-input"
                value={toDatetimeInput(draft.schedule.at)}
                onChange={(e) =>
                  setSchedule({ kind: "once", at: fromDatetimeInput(e.target.value) })
                }
              />
            </label>
          )}

          {draft.schedule.kind === "daily" && (
            <label className="automation-field">
              <span className="automation-field-label">每天执行时刻（本机时区）</span>
              <input
                type="time"
                className="automation-native-input"
                value={draft.schedule.time}
                onChange={(e) => setSchedule({ kind: "daily", time: e.target.value || "07:00" })}
              />
            </label>
          )}

          {draft.schedule.kind === "weekly" && (
            <>
              <div className="automation-field">
                <span className="automation-field-label">星期（可多选）</span>
                <Checkbox.Group
                  value={draft.schedule.weekdays}
                  options={WEEKDAY_NAMES.map((label, i) => ({ label, value: i + 1 }))}
                  onChange={(vals) =>
                    setSchedule({
                      kind: "weekly",
                      weekdays: (vals as number[]).slice().sort((a, b) => a - b),
                      time: draft.schedule.kind === "weekly" ? draft.schedule.time : "09:00",
                    })
                  }
                />
              </div>
              <label className="automation-field">
                <span className="automation-field-label">执行时刻（本机时区）</span>
                <input
                  type="time"
                  className="automation-native-input"
                  value={draft.schedule.kind === "weekly" ? draft.schedule.time : "09:00"}
                  onChange={(e) =>
                    setSchedule({
                      kind: "weekly",
                      weekdays:
                        draft.schedule.kind === "weekly" ? draft.schedule.weekdays : [1],
                      time: e.target.value || "09:00",
                    })
                  }
                />
              </label>
            </>
          )}

          {draft.schedule.kind === "interval" && (
            <label className="automation-field">
              <span className="automation-field-label">执行间隔</span>
              <span className="automation-interval">
                每{" "}
                <InputNumber
                  min={MIN_INTERVAL_MINUTES}
                  max={60 * 24 * 365}
                  value={draft.schedule.every_minutes}
                  onChange={(v) =>
                    setSchedule({
                      kind: "interval",
                      every_minutes: Math.max(MIN_INTERVAL_MINUTES, Number(v) || MIN_INTERVAL_MINUTES),
                    })
                  }
                />{" "}
                分钟（最小 {MIN_INTERVAL_MINUTES} 分钟）
              </span>
            </label>
          )}

          <div className="automation-preview">
            <div className="automation-preview-title">接下来 3 次执行（本机时区）</div>
            {preview.length === 0 ? (
              <div className="automation-preview-empty">当前规则没有后续执行时间，请调整时间</div>
            ) : (
              <ol>
                {preview.map((ts) => (
                  <li key={ts}>{fmtDateTime(ts)}</li>
                ))}
              </ol>
            )}
          </div>

          {task && (
            <div className="automation-field-hint">
              任务运行中修改配置不会打断当前一轮，保存后从下一轮开始生效。
            </div>
          )}
        </div>

        {assistantOpen && task && (
          <AutomationAssistant
            task={task}
            draft={draft}
            onApply={applyPatch}
            onClose={() => setAssistantOpen(false)}
          />
        )}
      </div>
    </Modal>
  );
}
