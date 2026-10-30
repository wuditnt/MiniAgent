/**
 * 系统配置弹窗：左上方齿轮图标打开，内含「模型配置」与「通用配置」两个页签。
 *
 * 为什么把两者合到一起？
 *   凡是「影响 Agent 怎么跑」的设置都收在这里，用户只需要记住一个入口；
 *   顶栏点模型名也能进来，只是直接落在「模型配置」页。
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Alert, Button, Input, InputNumber, Modal, Radio, Select, Spin, Switch, Tag } from "antd";
import {
  ApiOutlined,
  AppstoreOutlined,
  BulbOutlined,
  GlobalOutlined,
  ReloadOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { fetchSettings, fetchTools, updateSettings } from "../api";
import type {
  GeneralSettings,
  SandboxReadMode,
  SearchProvider,
  SettingsInfo,
  ToolInfo,
} from "../types";
import MemoryPanel from "./MemoryPanel";
import ModelSettingsPanel from "./ModelSettings";

interface Props {
  open: boolean;
  /** 打开时停在哪个页签 */
  tab: SettingsTab;
  onTab?: (t: SettingsTab) => void;
  onClose: () => void;
  /** 任意配置变更后通知 App 刷新 /api/state */
  onChanged: () => void;
  /** 当前工作区 id：记忆面板按工作区读取 / 保存项目记忆 */
  workspaceId: string;
}

export type SettingsTab = "model" | "general" | "search" | "memory" | "plugins";

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

/** 左侧导航的分组与条目（顺序即展示顺序） */
const NAV_GROUPS = ["设置", "数据与扩展"] as const;

const NAV: {
  key: SettingsTab;
  label: string;
  icon: ReactNode;
  group: (typeof NAV_GROUPS)[number];
}[] = [
  { key: "model", label: "模型配置", icon: <ApiOutlined />, group: "设置" },
  { key: "general", label: "通用配置", icon: <SettingOutlined />, group: "设置" },
  { key: "search", label: "联网搜索", icon: <GlobalOutlined />, group: "设置" },
  { key: "memory", label: "记忆", icon: <BulbOutlined />, group: "数据与扩展" },
  { key: "plugins", label: "插件管理", icon: <AppstoreOutlined />, group: "数据与扩展" },
];

/** 联网搜索后端选项：与聊天模型解耦，任何厂商的模型都能用 */
const SEARCH_BACKENDS: { value: SearchProvider; label: string; desc: string }[] = [
  {
    value: "deepseek",
    label: "DeepSeek 官方搜索（推荐）",
    desc:
      "调用 DeepSeek Search API（api.deepseek.com/v1/tools/search），使用下方独立配置的 DeepSeek 联网 Key（不复用「模型配置」里的 Key）；用其他厂商的模型也能联网搜索",
  },
  {
    value: "tavily",
    label: "Tavily",
    desc: "读取环境变量 TAVILY_API_KEY，面向 LLM 优化，返回质量稳定",
  },
  {
    value: "bocha",
    label: "博查",
    desc: "读取环境变量 BOCHA_API_KEY，国内合规、中文结果较好",
  },
  {
    value: "bing",
    label: "Bing 网页抓取",
    desc: "无需 Key，抓取 cn.bing.com 搜索结果页；页面结构变化时可能失效",
  },
];

export default function SettingsModal({ open, tab, onTab, onClose, onChanged, workspaceId }: Props) {
  const [info, setInfo] = useState<SettingsInfo | null>(null);
  const [draft, setDraft] = useState<GeneralSettings | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [active, setActive] = useState<SettingsTab>(tab);
  // DeepSeek 联网 Key 只写不读：输入框单独维护，为空表示「不修改」
  const [searchKey, setSearchKey] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    fetchSettings()
      .then((r) => {
        setInfo(r);
        setDraft(r.values);
        setSearchKey("");
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
    (!!draft && !!info && JSON.stringify(draft) !== JSON.stringify(info.values)) ||
    !!searchKey.trim();

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
      // Key 留空时不随草稿提交（后端会保留原值）
      const payload = searchKey.trim()
        ? { ...draft, deepseek_search_api_key: searchKey.trim() }
        : draft;
      const r = await updateSettings(payload);
      setInfo(r);
      setDraft(r.values);
      setSearchKey("");
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
      setSearchKey("");
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
      return <Switch checked={draft[key] as boolean} onChange={(v) => patch(key, v)} />;
    }
    return (
      <InputNumber
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
      {group === "context" && (
        <div className="gs-row">
          <div className="gs-label">
            <span className="gs-label-text">工具档位</span>
            <span className="gs-help">
              标准只常驻常用工具（文件 / 文档 / 代码 / 数据 / 联网 / 系统基础），
              每次请求省下约 1.5 万字符；需要低频能力（OCR、压缩包、数据库等）时
              模型会自动检索加载。「全部」一次性暴露所有工具
            </span>
          </div>
          <div className="gs-control">
            <Select
              value={draft?.tool_profile ?? "standard"}
              style={{ width: 200 }}
              options={[
                { value: "standard", label: "标准（省 token）" },
                { value: "full", label: "全部工具" },
              ]}
              onChange={(v) => patch("tool_profile", v)}
            />
          </div>
        </div>
      )}
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
      width={900}
      centered
      className="settings-modal"
      onCancel={onClose}
      destroyOnClose
      footer={
        active === "general" || active === "search"
          ? [
              <Button key="reset" icon={<ReloadOutlined />} onClick={reset} loading={saving}>
                恢复默认
              </Button>,
              <Button key="cancel" onClick={onClose}>
                关闭
              </Button>,
              <Button key="save" type="primary" onClick={save} loading={saving} disabled={!dirty}>
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
      <div className="settings-layout">
        <nav className="settings-nav">
          {NAV_GROUPS.map((group) => (
            <div className="settings-nav-group" key={group}>
              <div className="settings-nav-title">{group}</div>
              {NAV.filter((n) => n.group === group).map((n) => (
                <button
                  key={n.key}
                  type="button"
                  className={`settings-nav-item${active === n.key ? " active" : ""}`}
                  onClick={() => {
                    setActive(n.key);
                    onTab?.(n.key);
                  }}
                >
                  <span className="settings-nav-icon">{n.icon}</span>
                  {n.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="settings-content">
          {active === "model" && <ModelSettingsPanel onChanged={onChanged} />}
          {active === "search" && (
            <Spin spinning={loading}>
              {error && (
                <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />
              )}
              <div className="gs-group-title">联网搜索后端</div>
              {draft && (
                <div className="gs-section">
                  <div className="gs-row" style={{ alignItems: "flex-start" }}>
                    <div className="gs-label">
                      <span className="gs-label-text">搜索服务</span>
                      <span className="gs-help">
                        与聊天模型解耦：任何厂商的模型都能使用这里选定的搜索能力
                      </span>
                    </div>
                    <div className="gs-control">
                      <Select
                        value={draft.search_provider}
                        style={{ width: 260 }}
                        onChange={(v) => patch("search_provider", v)}
                        options={SEARCH_BACKENDS.map((b) => ({
                          value: b.value,
                          label: b.label,
                        }))}
                      />
                    </div>
                  </div>
                  <Alert
                    type="info"
                    showIcon
                    message={
                      SEARCH_BACKENDS.find((b) => b.value === draft.search_provider)?.desc || ""
                    }
                    style={{ marginTop: 10 }}
                  />
                </div>
              )}
              <div className="gs-group-title">DeepSeek 联网 Key</div>
              <div className="gs-section">
                <div className="gs-row" style={{ alignItems: "flex-start" }}>
                  <div className="gs-label">
                    <span className="gs-label-text">搜索 Key</span>
                    <span className="gs-help">
                      独立于「模型配置」：只用于 DeepSeek 搜索与网页抓取，留空表示不修改；
                      未配置时联网搜索不可用
                    </span>
                  </div>
                  <div className="gs-control">
                    <Input.Password
                      value={searchKey}
                      style={{ width: 320 }}
                      autoComplete="new-password"
                      placeholder={
                        info?.secrets?.deepseek_search_api_key
                          ? `已配置 ${info.secrets.deepseek_search_api_key}，留空不修改`
                          : "sk-..."
                      }
                      onChange={(e) => setSearchKey(e.target.value)}
                    />
                  </div>
                </div>
              </div>
              <div className="gs-group-title">使用方式</div>
              <div className="gs-section">
                <div className="gs-note" style={{ marginTop: 0 }}>
                  输入框右上角的「联网搜索」开关决定本轮的联网方式：
                  <br />
                  <b>开启</b>：<code>web_search</code> 用上面选定的搜索后端；
                  <code>fetch_url</code> 优先走 DeepSeek Fetch（返回 Markdown），
                  失败自动回退本地抓取。
                  <br />
                  <b>关闭</b>：不提供搜索工具；<code>fetch_url</code> 仅做本地直连抓取，
                  适合读取你明确给出的链接。
                  <br />
                  快速对话模式同样支持联网，只是不做推理思考。
                </div>
              </div>
              {dirty && (
                <Alert
                  type="info"
                  showIcon
                  message="改动尚未保存；保存后立即生效"
                  style={{ marginTop: 12 }}
                />
              )}
            </Spin>
          )}
          {active === "memory" && <MemoryPanel workspaceId={workspaceId} />}
          {active === "plugins" && (
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
          )}
          {active === "general" && (
            <Spin spinning={loading}>
              {error && (
                  <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />
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
                <div className="gs-group-title">运行日志</div>
                {draft && (
                  <div className="gs-section">
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 12,
                        padding: "8px 0",
                      }}
                    >
                      <div>
                        <div className="sandbox-opt-name">启用文件日志</div>
                        <div className="sandbox-opt-desc">
                          按天写入 logs/&lt;日期&gt;/ 下的 app.log、llm.log、tool.log、error.log（不含对话正文）
                        </div>
                      </div>
                      <Switch
                        checked={draft.log_enabled}
                        onChange={(v) => patch("log_enabled", v)}
                      />
                    </div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 12,
                        padding: "8px 0",
                      }}
                    >
                      <div>
                        <div className="sandbox-opt-name">日志级别</div>
                        <div className="sandbox-opt-desc">DEBUG 会额外记录每条 HTTP 请求</div>
                      </div>
                      <Select
                        value={draft.log_level}
                        style={{ width: 160 }}
                        disabled={!draft.log_enabled}
                        onChange={(v) => patch("log_level", v)}
                        options={[
                          { value: "DEBUG", label: "DEBUG" },
                          { value: "INFO", label: "INFO" },
                          { value: "WARNING", label: "WARNING" },
                          { value: "ERROR", label: "ERROR" },
                        ]}
                      />
                    </div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 12,
                        padding: "8px 0",
                      }}
                    >
                      <div>
                        <div className="sandbox-opt-name">保留天数</div>
                        <div className="sandbox-opt-desc">按天分目录，超过天数的旧目录自动清理</div>
                      </div>
                      <InputNumber
                        value={draft.log_retention_days}
                        min={1}
                        max={90}
                        disabled={!draft.log_enabled}
                        style={{ width: 160 }}
                        onChange={(v) => patch("log_retention_days", v)}
                      />
                    </div>
                  </div>
                )}
                <div className="gs-group-title">运行轨迹</div>
                {draft && (
                  <div className="gs-section">
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 12,
                        padding: "8px 0",
                      }}
                    >
                      <div>
                        <div className="sandbox-opt-name">记录会话事件流</div>
                        <div className="sandbox-opt-desc">
                          把每轮运行的 system prompt / 模型请求摘要 / 工具调用 / 授权决定等按序追加到
                          web_data/sessions/&lt;会话&gt;.events.jsonl，供「运行轨迹」抽屉回放；
                          关闭后不再产生新事件，对话本身不受影响
                        </div>
                      </div>
                      <Switch
                        checked={draft.events_enabled}
                        onChange={(v) => patch("events_enabled", v)}
                      />
                    </div>
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
          )}
        </div>
      </div>
    </Modal>
  );
}
