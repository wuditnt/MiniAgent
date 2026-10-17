/**
 * 系统配置弹窗：左上方齿轮图标打开，内含「模型配置」与「通用配置」两个页签。
 *
 * 为什么把两者合到一起？
 *   凡是「影响 Agent 怎么跑」的设置都收在这里，用户只需要记住一个入口；
 *   顶栏点模型名也能进来，只是直接落在「模型配置」页。
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, InputNumber, Modal, Radio, Spin, Switch, Tabs, Tag } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { fetchSettings, fetchTools, updateSettings } from "../api";
import type { GeneralSettings, SandboxReadMode, SettingsInfo, ToolInfo } from "../types";
import ModelSettingsPanel from "./ModelSettings";

interface Props {
  open: boolean;
  /** 打开时停在哪个页签 */
  tab: SettingsTab;
  /** 远程访问：配置只读，禁止保存（后端同样 403 拦截） */
  remote?: boolean;
  onTab?: (t: SettingsTab) => void;
  onClose: () => void;
  /** 任意配置变更后通知 App 刷新 /api/state */
  onChanged: () => void;
}

export type SettingsTab = "model" | "general" | "plugins";

/** 每个字段的界面元信息：标签、说明、区间、步进 */
const FIELDS: {
  key: keyof GeneralSettings;
  label: string;
  help: string;
  min: number;
  max: number;
  step?: number;
  group: "context" | "perf";
}[] = [
  {
    key: "max_turns",
    label: "最大轮数",
    help: "一次任务最多跑几轮（1 轮 = 一次模型调用 + 工具执行）",
    min: 1,
    max: 100,
    group: "context",
  },
  {
    key: "max_context_chars",
    label: "上下文字符上限",
    help: "超过就触发「摘要 + 滑动窗口」收敛，防止超出模型窗口",
    min: 2000,
    max: 2000000,
    step: 1000,
    group: "context",
  },
  {
    key: "keep_recent_turns",
    label: "保留最近轮数",
    help: "滑动窗口至少保留最近几轮原文，更早的会被摘要或裁掉",
    min: 1,
    max: 50,
    group: "context",
  },
  {
    key: "max_output_chars",
    label: "单次工具输出上限",
    help: "单个工具结果的字符上限，超出部分截断并提示",
    min: 500,
    max: 500000,
    step: 500,
    group: "context",
  },
  {
    key: "temperature",
    label: "温度",
    help: "0 更稳定、更贴近事实；调高更发散",
    min: 0,
    max: 2,
    step: 0.1,
    group: "perf",
  },
  {
    key: "max_workers",
    label: "并行工具线程数",
    help: "同一轮里多个工具并行执行的最大并发",
    min: 1,
    max: 16,
    group: "perf",
  },
];

export default function SettingsModal({ open, tab, remote = false, onTab, onClose, onChanged }: Props) {
  const [info, setInfo] = useState<SettingsInfo | null>(null);
  const [draft, setDraft] = useState<GeneralSettings | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [active, setActive] = useState<SettingsTab>(tab);

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    fetchSettings()
      .then((r) => {
        setInfo(r);
        setDraft(r.values);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const loadTools = useCallback(() => {
    setToolsLoading(true);
    fetchTools()
      .then((r) => setTools(r.tools))
      .catch(() => setTools([]))
      .finally(() => setToolsLoading(false));
  }, []);

  useEffect(() => {
    if (open) {
      setActive(tab);
      load();
      loadTools();
    }
  }, [open, tab, load, loadTools]);

  const dirty =
    !!draft && !!info && JSON.stringify(draft) !== JSON.stringify(info.values);

  const patch = (
    key: keyof GeneralSettings,
    value: number | boolean | string | string[] | null
  ) =>
    setDraft((d) => (d ? { ...d, [key]: value ?? d[key] } : d));

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setError("");
    try {
      const r = await updateSettings(draft);
      setInfo(r);
      setDraft(r.values);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setSaving(true);
    setError("");
    try {
      const r = await updateSettings({ reset: true });
      setInfo(r);
      setDraft(r.values);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const renderField = (key: keyof GeneralSettings) => {
    if (!draft) return null;
    const meta = FIELDS.find((f) => f.key === key)!;
    if (key === "stream" || key === "parallel_tools") {
      return <Switch disabled={remote} checked={draft[key] as boolean} onChange={(v) => patch(key, v)} />;
    }
    return (
      <InputNumber
        disabled={remote}
        value={draft[key] as number}
        min={meta.min}
        max={meta.max}
        step={meta.step}
        style={{ width: 160 }}
        onChange={(v) => patch(key, v)}
      />
    );
  };

  const section = (group: "context" | "perf") => (
    <div className="gs-section">
      {FIELDS.filter((f) => f.group === group).map((f) => (
        <div className="gs-row" key={f.key}>
          <div className="gs-label">
            <span className="gs-label-text">{f.label}</span>
            <span className="gs-help">{f.help}</span>
          </div>
          <div className="gs-control">{renderField(f.key)}</div>
        </div>
      ))}
      {group === "perf" && (
        <>
          <div className="gs-row">
            <div className="gs-label">
              <span className="gs-label-text">流式输出</span>
              <span className="gs-help">边生成边显示；关闭则等整段返回再渲染</span>
            </div>
            <div className="gs-control">{renderField("stream")}</div>
          </div>
          <div className="gs-row">
            <div className="gs-label">
              <span className="gs-label-text">并行工具执行</span>
              <span className="gs-help">同一轮的多个工具一起跑，关闭则改为串行</span>
            </div>
            <div className="gs-control">{renderField("parallel_tools")}</div>
          </div>
        </>
      )}
    </div>
  );

  return (
    <Modal
      open={open}
      title="系统配置"
      width={720}
      onCancel={onClose}
      destroyOnClose
      footer={
        active === "general"
          ? [
              <Button key="reset" icon={<ReloadOutlined />} onClick={reset} loading={saving} disabled={remote}>
                恢复默认
              </Button>,
              <Button key="cancel" onClick={onClose}>
                关闭
              </Button>,
              <Button key="save" type="primary" onClick={save} loading={saving} disabled={!dirty || remote}>
                保存并生效
              </Button>,
            ]
          : [
              <Button key="close" type="primary" onClick={onClose}>
                关闭
              </Button>,
            ]
      }
    >
      <Tabs
        activeKey={active}
        onChange={(k) => setActive(k as SettingsTab)}
        onTabClick={(k) => onTab?.(k as SettingsTab)}
        items={[
          {
            key: "model",
            label: "模型配置",
            children: <ModelSettingsPanel onChanged={onChanged} remote={remote} />,
          },
          {
            key: "general",
            label: "通用配置",
            children: (
              <Spin spinning={loading}>
                {error && (
                  <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />
                )}
                {remote && (
                  <Alert
                    type="info"
                    showIcon
                    message="远程访问模式：配置仅供查看，修改请在主机本机进行"
                    style={{ marginBottom: 12 }}
                  />
                )}
                <div className="gs-group-title">对话与上下文</div>
                {section("context")}
                <div className="gs-group-title">生成与性能</div>
                {section("perf")}
                <div className="gs-group-title">沙箱读取范围</div>
                {draft && (
                  <div className="gs-section">
                    <Radio.Group
                      className="sandbox-radio"
                      disabled={remote}
                      value={draft.sandbox_read || "strict"}
                      onChange={(e) =>
                        patch("sandbox_read", e.target.value as SandboxReadMode)
                      }
                    >
                      <Radio value="strict">
                        <span className="sandbox-opt-name">严格隔离（默认）</span>
                        <span className="sandbox-opt-desc">工作区之外的路径一律不可读</span>
                      </Radio>
                      <Radio value="whitelist">
                        <span className="sandbox-opt-name">白名单只读</span>
                        <span className="sandbox-opt-desc">仅下方白名单内的绝对路径可读</span>
                      </Radio>
                      <Radio value="open">
                        <span className="sandbox-opt-name">全部只读放开</span>
                        <span className="sandbox-opt-desc">可读取工作区外任意路径（仍不可写）</span>
                      </Radio>
                    </Radio.Group>
                    {draft.sandbox_read === "whitelist" && (
                      <Input.TextArea
                        className="sandbox-whitelist"
                        disabled={remote}
                        value={(draft.sandbox_read_whitelist || []).join("\n")}
                        onChange={(e) =>
                          patch(
                            "sandbox_read_whitelist",
                            e.target.value
                              .split(/[\r\n,]+/)
                              .map((s) => s.trim())
                              .filter(Boolean)
                          )
                        }
                        rows={4}
                        placeholder={"每行一个绝对路径，例如：\nC:\\Users\\me\\Documents\\share\nD:\\data"}
                      />
                    )}
                    <Alert
                      type="warning"
                      showIcon
                      message="硬规则：任何模式下，工作区之外都禁止写入、删除、移动。读取放开只影响读，不影响写。"
                      style={{ marginTop: 10 }}
                    />
                  </div>
                )}
                {dirty && (
                  <Alert
                    type="info"
                    showIcon
                    message="改动尚未保存；保存后立即生效（正在执行的任务不受影响）"
                    style={{ marginTop: 12 }}
                  />
                )}
                <div className="gs-note">
                  取值优先级：命令行显式参数 &gt; 这里保存的值 &gt; 环境变量 &gt; 内置默认。
                </div>
              </Spin>
            ),
          },
          {
            key: "plugins",
            label: "插件管理",
            children: (
              <Spin spinning={toolsLoading}>
                <div className="plugins-list">
                  {tools.map((t) => (
                    <div className="plugin-item" key={t.name}>
                      <div className="plugin-header">
                        <span className="plugin-name">{t.name}</span>
                        <Tag color={t.source === "core" ? "blue" : t.source === "builtin" ? "green" : t.source === "user" ? "orange" : "purple"}>
                          {t.source}
                        </Tag>
                        {t.dangerous && <Tag color="red">危险</Tag>}
                        {t.exclusive && <Tag color="cyan">独占</Tag>}
                      </div>
                      <div className="plugin-desc">{t.description}</div>
                      <div className="plugin-meta">
                        <span className="plugin-sig">{t.signature}</span>
                        <span className="plugin-group">分组：{t.group}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </Spin>
            ),
          },
        ]}
      />
    </Modal>
  );
}
