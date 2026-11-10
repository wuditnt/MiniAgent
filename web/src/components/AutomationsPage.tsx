/**
 * 自动化任务页：任务列表 + 空状态模板 + 新建/编辑入口。
 *
 * 交互要点：
 *  · 进入页面即拉取任务，之后每 10s 静默刷新（运行状态与下次执行时间）
 *  · 空状态展示 8 个内置模板，点击预填创建表单
 *  · 删除只删任务与执行历史，会话保留（文案里明确说明）
 *  · 远程访问只读：隐藏新建/编辑/删除/立即执行
 */
import { useCallback, useEffect, useState } from "react";
import {
  DeleteOutlined,
  EditOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Button, Empty, Popconfirm, Switch, Tag, Tooltip, message } from "antd";
import type { AutomationTask, ModelProvider, WorkspaceItem } from "../types";
import { deleteAutomation, fetchAutomations, runAutomation, updateAutomation } from "../api";
import AutomationEditor from "./AutomationEditor";
import {
  AUTOMATION_TEMPLATES,
  STATUS_LABELS,
  describeSchedule,
  fmtDateTime,
  fmtRelative,
} from "./automationTemplates";

interface Props {
  workspaces: WorkspaceItem[];
  models: ModelProvider[];
  /** 当前工作区：模板创建时的默认归属 */
  workspaceId: string;
  remote?: boolean;
  /** 打开任务产生的会话（跳回对话页） */
  onOpenSession: (sid: string) => void;
}

interface EditorState {
  open: boolean;
  task: AutomationTask | null;
  initial: Record<string, unknown> | null;
}

const STATUS_COLOR: Record<string, string> = {
  success: "green",
  failed: "red",
  skipped: "orange",
  aborted: "default",
};

export default function AutomationsPage({
  workspaces,
  models,
  workspaceId,
  remote,
  onOpenSession,
}: Props) {
  const [tasks, setTasks] = useState<AutomationTask[]>([]);
  const [running, setRunning] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<EditorState>({ open: false, task: null, initial: null });
  const [busyId, setBusyId] = useState("");

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await fetchAutomations();
      setTasks(data.tasks);
      setRunning(data.running || {});
    } catch (e) {
      if (!silent) message.error((e as Error).message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(() => load(true), 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  const openCreate = (initial: Record<string, unknown> | null = null) => {
    setEditor({ open: true, task: null, initial });
  };

  const handleToggle = async (task: AutomationTask, enabled: boolean) => {
    setBusyId(task.id);
    try {
      const updated = await updateAutomation(task.id, { enabled });
      setTasks((list) => list.map((t) => (t.id === updated.id ? updated : t)));
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusyId("");
    }
  };

  const handleRun = async (task: AutomationTask) => {
    setBusyId(task.id);
    try {
      await runAutomation(task.id);
      message.success("已开始执行，稍后可在任务生成的会话中查看结果");
      await load(true);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusyId("");
    }
  };

  const handleDelete = async (task: AutomationTask) => {
    try {
      await deleteAutomation(task.id);
      setTasks((list) => list.filter((t) => t.id !== task.id));
      message.success("任务已删除；生成的会话与历史消息已保留");
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const handleSaved = (task: AutomationTask) => {
    setTasks((list) => {
      const exists = list.some((t) => t.id === task.id);
      return exists ? list.map((t) => (t.id === task.id ? task : t)) : [task, ...list];
    });
    setEditor({ open: false, task: null, initial: null });
  };

  return (
    <div className="page automations-page">
      <header className="page-head">
        <div className="page-head-left">
          <ThunderboltOutlined className="page-title-icon" />
          <span className="page-title">自动化</span>
          <span className="page-sub">到点自动执行一次任务，结果写入同一条会话；应用关闭期间错过的排期不会补跑</span>
        </div>
        <div className="page-head-right">
          <Tooltip title="刷新">
            <Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading} />
          </Tooltip>
          {!remote && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate()}>
              新建任务
            </Button>
          )}
        </div>
      </header>

      <div className="page-body">
        {remote && <div className="automation-remote-tip">远程访问为只读模式，只能查看任务与结果。</div>}

      {!loading && tasks.length === 0 && (
        <div className="automation-empty">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有自动化任务，从下面的模板开始，或直接新建一个"
          />
          <div className="automation-templates">
            {AUTOMATION_TEMPLATES.map((tpl) => (
              <button
                key={tpl.key}
                className="template-card"
                disabled={!!remote}
                onClick={() =>
                  openCreate({
                    name: tpl.name,
                    prompt: tpl.prompt,
                    schedule: tpl.schedule,
                    web: tpl.web,
                    workspace_id: workspaceId,
                  })
                }
              >
                <div className="template-card-title">
                  {tpl.name}
                  {tpl.web && <Tag color="blue">联网</Tag>}
                  {tpl.needsRepo && <Tag>需代码仓库</Tag>}
                </div>
                <div className="template-card-desc">{tpl.desc}</div>
                <div className="template-card-meta">{describeSchedule(tpl.schedule)}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="automation-list">
        {tasks.map((task) => {
          const isRunning = !!running[task.id];
          return (
            <div className={`automation-card ${task.enabled ? "" : "disabled"}`} key={task.id}>
              <div className="automation-card-head">
                <div className="automation-card-title">
                  {task.name}
                  {isRunning && <span className="automation-running">执行中</span>}
                  {!task.enabled && <Tag>已停用</Tag>}
                </div>
                <div className="automation-card-head-actions">
                  <Tooltip title={task.enabled ? "停用后不再按排期执行" : "启用后从下一个周期开始执行"}>
                    <Switch
                      size="small"
                      checked={task.enabled}
                      loading={busyId === task.id}
                      disabled={!!remote}
                      onChange={(v) => handleToggle(task, v)}
                    />
                  </Tooltip>
                </div>
              </div>

              <div className="automation-card-prompt">{task.prompt}</div>

              <div className="automation-card-meta">
                <span>
                  <b>规则</b> {task.schedule_text}
                </span>
                <span>
                  <b>下次</b>{" "}
                  {task.enabled ? (
                    <Tooltip title={fmtDateTime(task.next_run_at)}>
                      {fmtRelative(task.next_run_at)}
                    </Tooltip>
                  ) : (
                    "未排期"
                  )}
                </span>
                <span>
                  <b>最近</b>{" "}
                  {task.last_status ? (
                    <>
                      <Tag color={STATUS_COLOR[task.last_status] || "default"}>
                        {STATUS_LABELS[task.last_status] || task.last_status}
                      </Tag>
                      {task.last_run_at ? fmtRelative(task.last_run_at) : ""}
                    </>
                  ) : (
                    "尚未执行"
                  )}
                </span>
                <span>
                  <b>已执行</b> {task.run_count} 次
                  {task.missed > 0 ? ` · 错过 ${task.missed} 次` : ""}
                </span>
              </div>

              {task.last_error && <div className="automation-card-error">{task.last_error}</div>}

              <div className="automation-card-actions">
                {task.session_id && (
                  <Button size="small" type="link" onClick={() => onOpenSession(task.session_id)}>
                    查看会话
                  </Button>
                )}
                <div className="automation-card-actions-right">
                  <Button
                    size="small"
                    icon={<PlayCircleOutlined />}
                    disabled={!!remote || isRunning}
                    loading={busyId === task.id && isRunning}
                    onClick={() => handleRun(task)}
                  >
                    立即执行
                  </Button>
                  <Button
                    size="small"
                    icon={<EditOutlined />}
                    disabled={!!remote}
                    onClick={() => setEditor({ open: true, task, initial: null })}
                  >
                    编辑
                  </Button>
                  {!remote && (
                    <Popconfirm
                      title="删除这个任务？"
                      description="只删除任务与执行历史，任务生成的会话与历史消息会保留。"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={() => handleDelete(task)}
                    >
                      <Button size="small" danger icon={<DeleteOutlined />}>
                        删除
                      </Button>
                    </Popconfirm>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {!loading && tasks.length > 0 && !remote && (
        <div className="automation-footer-tip">
          <RobotOutlined /> 想要更复杂的调度？打开某个任务的「编辑」，用 AI 助手直接说「改成每周三下午三点」即可。
        </div>
      )}

      <AutomationEditor
        open={editor.open}
        task={editor.task}
        initial={editor.initial}
        workspaces={workspaces}
        models={models}
        remote={remote}
        onClose={() => setEditor({ open: false, task: null, initial: null })}
        onSaved={handleSaved}
      />
      </div>
    </div>
  );
}
