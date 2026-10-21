/**
 * 记忆管理面板（系统配置 → 记忆）：跨工作区的用户画像 + 记忆总开关。
 *
 * 项目记忆与主题摘要是按工作区隔离的，入口已移到左侧抽屉
 * 「每个工作区的快捷工具 → 工作区记忆」（WorkspaceMemoryModal）。
 *
 * 为什么用户画像与项目记忆分开？
 *   前者跨工作区（存在 web_data/memory/），后者随项目文件夹走
 *   （<工作区>/.memory/project.md，默认隐藏）。两者的生命周期与迁移语义
 *   完全不同，混在一起容易让人误以为「换个工作区偏好就丢了」。
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, InputNumber, Spin, Switch } from "antd";
import { ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import {
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
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    Promise.all([fetchMemory(workspaceId), fetchSettings()])
      .then(([m, s]) => {
        setData(m);
        setDraft(m.user_profile);
        setConf(s.values);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [workspaceId]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await saveMemory("user", draft, workspaceId);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
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
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={5}
          placeholder="例如：- 2026-10-17 10:00 · 手动 — 回答尽量简洁，不要罗列冗余选项"
        />
        <div className="mem-actions">
          <Button
            type="primary"
            size="small"
            icon={<SaveOutlined />}
            loading={saving}
            disabled={draft === (data?.user_profile ?? "")}
            onClick={save}
          >
            保存
          </Button>
        </div>
      </div>
    </Spin>
  );
}
