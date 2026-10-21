/**
 * 工作区记忆（独立页面）：某个工作区的「项目记忆 + 主题摘要」。
 *
 * 入口在左侧抽屉每个工作区的快捷工具区（改名按钮左边）。
 * 用户画像与记忆总开关仍留在「系统配置 → 记忆」，因为它们跨工作区、与单个项目无关。
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Collapse, Empty, Input, Modal, Popconfirm, Spin } from "antd";
import { DeleteOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { deleteMemoryTopic, fetchMemory, saveMemory } from "../api";
import type { MemoryData, WorkspaceItem } from "../types";

interface Props {
  open: boolean;
  workspace: WorkspaceItem | null;
  onClose: () => void;
}

export default function WorkspaceMemoryModal({ open, workspace, onClose }: Props) {
  const [data, setData] = useState<MemoryData | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const wid = workspace?.id || "";

  const load = useCallback(() => {
    if (!wid) return;
    setLoading(true);
    setError("");
    fetchMemory(wid)
      .then((m) => {
        setData(m);
        setDraft(m.project);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [wid]);

  // 打开弹窗或切换工作区时重新读取
  useEffect(() => {
    if (!open) return;
    setData(null);
    setDraft("");
    load();
  }, [open, load]);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await saveMemory("project", draft, wid);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const removeTopic = async (sid: string) => {
    try {
      await deleteMemoryTopic(sid, wid);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Modal
      open={open}
      title="工作区记忆"
      footer={null}
      width={720}
      onCancel={onClose}
      destroyOnClose
      className="ws-memory-modal"
    >
      <Spin spinning={loading}>
        <div className="ws-memory-head">
          <span className="ws-memory-name">{workspace?.name || ""}</span>
          <span className="ws-memory-path">{data?.workspace_root || workspace?.path || ""}</span>
        </div>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

        <div className="gs-group-title">
          项目记忆<span className="mem-scope">随当前工作区文件夹保存</span>
          <Button
            size="small"
            type="text"
            icon={<ReloadOutlined />}
            onClick={load}
            title="重新读取"
          />
        </div>
        <div className="mem-editor">
          <div className="mem-path">{data?.project_path || "<工作区>/.memory/project.md"}</div>
          <Input.TextArea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={8}
            placeholder="例如：- 2026-10-17 10:00 · 手动 — 后端改完必须杀 python 进程再重启"
          />
          <div className="mem-actions">
            <Button
              type="primary"
              size="small"
              icon={<SaveOutlined />}
              loading={saving}
              disabled={draft === (data?.project ?? "")}
              onClick={save}
            >
              保存
            </Button>
          </div>
        </div>

        <div className="gs-group-title">
          主题摘要<span className="mem-scope">模型自动提炼，保留最近 20 条</span>
        </div>
        {data && data.topics.length > 0 ? (
          <Collapse
            className="mem-topics"
            size="small"
            items={data.topics.map((t) => ({
              key: t.session,
              label: (
                <span className="mem-topic-label">
                  <span className="mem-topic-name">{t.session}</span>
                  <span className="mem-topic-meta">
                    {t.mtime_text} · {t.size} B
                  </span>
                </span>
              ),
              extra: (
                <Popconfirm
                  title="删除这条摘要？"
                  description="记忆中的会话摘要会被移除，不影响会话本身"
                  okText="删除"
                  cancelText="取消"
                  onConfirm={(e) => {
                    e?.stopPropagation();
                    removeTopic(t.session);
                  }}
                  onCancel={(e) => e?.stopPropagation()}
                >
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={(e) => e.stopPropagation()}
                  />
                </Popconfirm>
              ),
              children: <pre className="mem-topic-body">{t.content}</pre>,
            }))}
          />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有摘要，对话几轮后自动生成" />
        )}
      </Spin>
    </Modal>
  );
}
