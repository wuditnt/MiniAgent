/**
 * 记忆管理面板：查看 / 编辑三层记忆，并控制记忆开关。
 *
 * 为什么用户画像与项目记忆分开两块？
 *   前者跨工作区（存在 web_data/memory/），后者随项目文件夹走
 *   （<工作区>/.memory/project.md，默认隐藏）。两者的生命周期与迁移语义
 *   完全不同，混在一起容易让人误以为「换个工作区偏好就丢了」。
 *
 * 主题摘要只读 + 可删：它们是模型自动提炼的滚动摘要，手改意义不大，
 * 但用户有权清掉不想留的内容。
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Collapse, Empty, Input, InputNumber, Popconfirm, Spin, Switch } from "antd";
import { DeleteOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import {
  deleteMemoryTopic,
  fetchMemory,
  fetchSettings,
  saveMemory,
  updateSettings,
} from "../api";
import type { GeneralSettings, MemoryData } from "../types";

interface Props {
  workspaceId: string;
}

export default function MemoryPanel({ workspaceId }: Props) {
  const [data, setData] = useState<MemoryData | null>(null);
  const [conf, setConf] = useState<GeneralSettings | null>(null);
  const [draft, setDraft] = useState<{ user: string; project: string }>({
    user: "",
    project: "",
  });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<"" | "user" | "project">("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    Promise.all([fetchMemory(workspaceId), fetchSettings()])
      .then(([m, s]) => {
        setData(m);
        setDraft({ user: m.user_profile, project: m.project });
        setConf(s.values);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async (scope: "user" | "project") => {
    setSaving(scope);
    setError("");
    try {
      await saveMemory(scope, draft[scope], workspaceId);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving("");
    }
  };

  /** 开关类配置：立即生效，不参与文本域的草稿状态 */
  const setConfValue = async (patch: Partial<GeneralSettings>) => {
    if (!conf) return;
    setConf({ ...conf, ...patch });
    try {
      const r = await updateSettings(patch);
      setConf(r.values);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      load();
    }
  };

  const removeTopic = async (sid: string) => {
    try {
      await deleteMemoryTopic(sid, workspaceId);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Spin spinning={loading}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

      <div className="gs-section">
        <div className="gs-row">
          <div className="gs-label">
            <span className="gs-label-text">启用记忆</span>
            <span className="gs-help">
              关闭后不再注入、不自动提炼，模型也看不到 remember 工具
            </span>
          </div>
          <div className="gs-control">
            <Switch
              checked={!!conf?.memory_enabled}
              onChange={(v) => setConfValue({ memory_enabled: v })}
            />
          </div>
        </div>
        <div className="gs-row">
          <div className="gs-label">
            <span className="gs-label-text">自动提炼</span>
            <span className="gs-help">
              每轮回复后由模型提炼偏好/决定/坑与摘要；关掉则只保留手动写入
            </span>
          </div>
          <div className="gs-control">
            <Switch
              checked={!!conf?.memory_auto_extract}
              disabled={!conf?.memory_enabled}
              onChange={(v) => setConfValue({ memory_auto_extract: v })}
            />
          </div>
        </div>
        <div className="gs-row">
          <div className="gs-label">
            <span className="gs-label-text">注入预算</span>
            <span className="gs-help">每轮拼进系统提示的记忆字符上限，超出先裁剪历史摘要</span>
          </div>
          <div className="gs-control">
            <InputNumber
              value={conf?.memory_budget_chars}
              min={200}
              max={8000}
              step={100}
              style={{ width: 160 }}
              disabled={!conf?.memory_enabled}
              onChange={(v) => v && setConfValue({ memory_budget_chars: v })}
            />
          </div>
        </div>
      </div>

      <div className="gs-group-title">
        用户画像<span className="mem-scope">跨所有工作区</span>
        <Button
          size="small"
          type="text"
          icon={<ReloadOutlined />}
          onClick={load}
          title="重新读取"
        />
      </div>
      <div className="mem-editor">
        <div className="mem-path">{data?.user_profile_path || "web_data/memory/user_profile.md"}</div>
        <Input.TextArea
          value={draft.user}
          onChange={(e) => setDraft((d) => ({ ...d, user: e.target.value }))}
          rows={5}
          placeholder="例如：- 2026-10-17 10:00 · 手动 — 回答尽量简洁，不要罗列冗余选项"
        />
        <div className="mem-actions">
          <Button
            type="primary"
            size="small"
            icon={<SaveOutlined />}
            loading={saving === "user"}
            disabled={draft.user === (data?.user_profile ?? "")}
            onClick={() => save("user")}
          >
            保存
          </Button>
        </div>
      </div>

      <div className="gs-group-title">
        项目记忆<span className="mem-scope">随当前工作区文件夹保存</span>
      </div>
      <div className="mem-editor">
        <div className="mem-path">{data?.project_path || "<工作区>/.memory/project.md"}</div>
        <Input.TextArea
          value={draft.project}
          onChange={(e) => setDraft((d) => ({ ...d, project: e.target.value }))}
          rows={6}
          placeholder="例如：- 2026-10-17 10:00 · 手动 — 后端改完必须杀 python 进程再重启"
        />
        <div className="mem-actions">
          <Button
            type="primary"
            size="small"
            icon={<SaveOutlined />}
            loading={saving === "project"}
            disabled={draft.project === (data?.project ?? "")}
            onClick={() => save("project")}
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
  );
}
