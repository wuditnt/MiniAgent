/**
 * 连接页（占满主区，包含原本右侧的工作区文件区）：
 * 把外部 MCP server 接入 mini-agent。
 *
 * 第一版只支持 stdio（本机命令），数据来自 /api/connectors。
 * 写操作仅本机 + 超级管理员可用，远程访问时只读。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import {
  ApiOutlined,
  ArrowLeftOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  ImportOutlined,
  MinusCircleFilled,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Drawer,
  Dropdown,
  Empty,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Spin,
  Switch,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { MenuProps } from "antd";
import {
  deleteConnector,
  exportConnectors,
  fetchConnectorPresets,
  fetchConnectors,
  importConnectors,
  retryConnector,
  saveConnector,
  testConnector,
  toggleConnector,
} from "../api";
import type {
  ConnectorInfo,
  ConnectorPreset,
  ConnectorRuntime,
  ConnectorSpec,
  ConnectorTestResult,
} from "../types";
import "./connectors.css";

interface Props {
  /** 返回对话页 */
  onClose: () => void;
  /** 远程访问：只读，隐藏所有写操作 */
  remote?: boolean;
}

interface EnvRow {
  k: string;
  v: string;
}

interface Draft {
  name: string;
  display_name: string;
  description: string;
  transport: "stdio" | "http";
  command: string;
  argsText: string;
  url: string;
  headers: EnvRow[];
  cwd: string;
  timeout: number;
  enabled: boolean;
  dangerous: boolean;
  env: EnvRow[];
  /** 被关闭的工具（服务端原始名） */
  disabledTools: string[];
}

const NAME_RE = /^[A-Za-z0-9_-]{1,40}$/;

function draftFrom(c?: ConnectorInfo): Draft {
  return {
    name: c?.name || "",
    display_name: c?.display_name || "",
    description: c?.description || "",
    transport: c?.transport || "stdio",
    command: c?.transport === "http" ? "" : c?.command || "",
    argsText: (c?.args || []).join("\n"),
    url: c?.url || "",
    headers: Object.entries(c?.headers || {}).map(([k, v]) => ({ k, v })),
    cwd: c?.cwd || "",
    timeout: c?.timeout || 30,
    enabled: c?.enabled ?? true,
    dangerous: c?.dangerous ?? true,
    env: Object.entries(c?.env || {}).map(([k, v]) => ({ k, v })),
    disabledTools: [...(c?.disabled_tools || [])],
  };
}

function specFrom(draft: Draft): ConnectorSpec {
  const env: Record<string, string> = {};
  for (const row of draft.env) {
    const key = row.k.trim();
    if (key) env[key] = row.v;
  }
  const headers: Record<string, string> = {};
  for (const row of draft.headers) {
    const key = row.k.trim();
    if (key) headers[key] = row.v;
  }
  return {
    name: draft.name.trim(),
    display_name: draft.display_name.trim() || draft.name.trim(),
    description: draft.description.trim(),
    transport: draft.transport,
    command: draft.transport === "stdio" ? draft.command.trim() : "",
    args: draft.argsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean),
    url: draft.transport === "http" ? draft.url.trim() : "",
    headers: draft.transport === "http" ? headers : {},
    cwd: draft.cwd.trim(),
    env,
    timeout: draft.timeout,
    enabled: draft.enabled,
    dangerous: draft.dangerous,
    disabled_tools: [...draft.disabledTools],
  };
}

function statusOf(c: ConnectorInfo): { label: string; cls: string; icon: ReactNode } {
  if (!c.enabled) {
    return { label: "已禁用", cls: "off", icon: <MinusCircleFilled /> };
  }
  if (c.status === "ready") {
    return { label: "已连接", cls: "ok", icon: <CheckCircleFilled /> };
  }
  if (c.status === "failed") {
    return { label: "连接失败", cls: "err", icon: <CloseCircleFilled /> };
  }
  return { label: "未启动", cls: "idle", icon: <MinusCircleFilled /> };
}

export default function ConnectorsPage({ onClose, remote }: Props) {
  const [loading, setLoading] = useState(false);
  const [runtime, setRuntime] = useState<ConnectorRuntime | null>(null);
  const [mcpEnabled, setMcpEnabled] = useState(true);
  const [running, setRunning] = useState(0);
  const [items, setItems] = useState<ConnectorInfo[]>([]);
  const [query, setQuery] = useState("");

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ConnectorInfo | null>(null);
  const [draft, setDraft] = useState<Draft>(draftFrom());
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ConnectorTestResult | null>(null);
  const [deleting, setDeleting] = useState<ConnectorInfo | null>(null);
  const [presets, setPresets] = useState<ConnectorPreset[]>([]);
  const [presetHint, setPresetHint] = useState("");
  const [retrying, setRetrying] = useState("");
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const locked = remote || !mcpEnabled || running > 0;

  async function reload() {
    setLoading(true);
    try {
      const data = await fetchConnectors();
      setItems(data.connectors);
      setRuntime(data.runtime);
      setMcpEnabled(data.mcp_enabled);
      setRunning(data.running || 0);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  // 页面挂载即拉取（不再依赖 open 开关）
  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 模板清单随包发布，只读；远程访问也展示（禁用写操作即可）
  useEffect(() => {
    fetchConnectorPresets()
      .then(setPresets)
      .catch(() => setPresets([]));
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((c) =>
      [c.display_name, c.name, c.description, c.command]
        .join(" ")
        .toLowerCase()
        .includes(q)
    );
  }, [items, query]);

  function openCreate() {
    setEditing(null);
    setDraft(draftFrom());
    setTestResult(null);
    setPresetHint("");
    setEditorOpen(true);
  }

  function openEdit(c: ConnectorInfo) {
    setEditing(c);
    setDraft(draftFrom(c));
    setTestResult(null);
    setPresetHint("");
    setEditorOpen(true);
  }

  /** 模板只预填表单：名称冲突时自动加序号，参数里的路径需要用户自己改 */
  function openPreset(p: ConnectorPreset) {
    let name = p.key;
    let n = 2;
    while (items.some((c) => c.name === name)) name = `${p.key}-${n++}`;
    setEditing(null);
    setDraft({
      ...draftFrom(),
      name,
      display_name: p.display_name,
      description: p.description,
      command: p.command,
      argsText: p.args.join("\n"),
      timeout: p.key === "puppeteer" ? 120 : 30,
      dangerous: p.dangerous,
    });
    setTestResult(null);
    setPresetHint(p.hint || "");
    setEditorOpen(true);
  }

  async function onRetry(c: ConnectorInfo) {
    setRetrying(c.name);
    try {
      await retryConnector(c.name);
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setRetrying("");
    }
  }

  async function onExport() {
    try {
      const data = await exportConnectors();
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `miniagent-connectors-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      message.success("已导出（环境变量已脱敏）");
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function onImportFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    let servers: Record<string, unknown>;
    try {
      const data = JSON.parse(await file.text());
      servers = data?.servers;
      if (!servers || typeof servers !== "object") {
        message.error("文件格式不正确：缺少 servers 对象");
        return;
      }
    } catch (err) {
      message.error(`读取文件失败：${(err as Error).message}`);
      return;
    }
    setImporting(true);
    try {
      const res = await importConnectors(servers);
      const ok = res.results.filter((r) => r.ok).length;
      if (ok) message.success(`已导入 ${ok} 个连接器`);
      const failed = res.results.filter((r) => !r.ok);
      if (failed.length) {
        message.warning(
          `${failed.length} 个失败：${failed.map((r) => `${r.name}（${r.error}）`).join("；")}`
        );
      }
      for (const w of res.warnings || []) message.warning(w);
      await reload();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setImporting(false);
    }
  }

  async function onTest() {
    const spec = specFrom(draft);
    if (!spec.name || !NAME_RE.test(spec.name)) {
      message.warning("请先填写合法的连接器标识");
      return;
    }
    if (spec.transport === "stdio" && !spec.command) {
      message.warning("请先填写启动命令");
      return;
    }
    if (spec.transport === "http" && !spec.url) {
      message.warning("请先填写服务地址");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testConnector(spec));
    } catch (e) {
      setTestResult({ ok: false, error: (e as Error).message, stderr: [] });
    } finally {
      setTesting(false);
    }
  }

  async function onSave() {
    const spec = specFrom(draft);
    if (!NAME_RE.test(spec.name)) {
      message.warning("标识只能包含字母、数字、下划线或短横线，长度 1-40");
      return;
    }
    if (spec.transport === "stdio" && !spec.command) {
      message.warning("启动命令不能为空");
      return;
    }
    if (spec.transport === "http" && !spec.url) {
      message.warning("服务地址不能为空");
      return;
    }
    setSaving(true);
    try {
      await saveConnector(spec);
      message.success("已保存，工具已热加载");
      setEditorOpen(false);
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function onToggle(c: ConnectorInfo, enabled: boolean) {
    try {
      await toggleConnector(c.name, enabled);
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function onDelete() {
    if (!deleting) return;
    try {
      await deleteConnector(deleting.name);
      message.success("已删除");
      setDeleting(null);
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  function onCardMenu(c: ConnectorInfo): MenuProps {
    return {
      items: [
        { key: "edit", icon: <EditOutlined />, label: "编辑" },
        {
          key: "toggle",
          icon: c.enabled ? <MinusCircleFilled /> : <ThunderboltOutlined />,
          label: c.enabled ? "禁用" : "启用",
        },
        { type: "divider" },
        { key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true },
      ],
      onClick: ({ key, domEvent }) => {
        domEvent.stopPropagation();
        if (key === "edit") openEdit(c);
        if (key === "toggle") void onToggle(c, !c.enabled);
        if (key === "delete") setDeleting(c);
      },
    };
  }

  const needsNpx = /^npx(\.cmd)?$/i.test(draft.command.trim());
  const nodeMissing = needsNpx && !runtime?.npx.found;

  return (
    <div className="page connectors-page">
      <header className="page-head">
        <div className="page-head-left">
          <Tooltip title="返回对话">
            <Button type="text" icon={<ArrowLeftOutlined />} onClick={onClose} />
          </Tooltip>
          <ApiOutlined className="page-title-icon" />
          <span className="page-title">连接</span>
          <span className="page-sub">把外部 MCP 能力接入 mini-agent，连接一次，所有会话可用</span>
        </div>
        <div className="page-head-right">
          <Tooltip
            title={
              runtime?.node.found
                ? `Node ${runtime.node.version}（${runtime.node.path}）`
                : "未检测到 Node.js，npx 类连接器无法启动"
            }
          >
            <Tag
              className={`connectors-runtime ${runtime?.node.found ? "ok" : "warn"}`}
              icon={runtime?.node.found ? <CheckCircleFilled /> : <WarningOutlined />}
            >
              {runtime?.node.found ? "Node 可用" : "未检测到 Node"}
            </Tag>
          </Tooltip>
          <Button icon={<ReloadOutlined />} onClick={() => void reload()} loading={loading}>
            刷新
          </Button>
          <Dropdown
            trigger={["click"]}
            disabled={!presets.length}
            menu={{
              items: presets.map((p) => ({
                key: p.key,
                label: (
                  <div className="connector-preset-item">
                    <span>{p.display_name}</span>
                    <span className="connector-preset-desc">{p.description}</span>
                  </div>
                ),
              })),
              onClick: ({ key }) => {
                const p = presets.find((x) => x.key === key);
                if (p) openPreset(p);
              },
            }}
          >
            <Button icon={<PlusOutlined />} disabled={locked}>
              从模板添加
            </Button>
          </Dropdown>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} disabled={locked}>
            自定义连接器
          </Button>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "export", icon: <ExportOutlined />, label: "导出配置" },
                {
                  key: "import",
                  icon: <ImportOutlined />,
                  label: "导入配置",
                  disabled: locked || importing,
                },
              ],
              onClick: ({ key }) => {
                if (key === "export") void onExport();
                else fileRef.current?.click();
              },
            }}
          >
            <Button icon={<MoreOutlined />} />
          </Dropdown>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            style={{ display: "none" }}
            onChange={(e) => void onImportFile(e)}
          />
        </div>
      </header>

      <div className="page-body">
        {!mcpEnabled && (
          <Alert
            type="warning"
            showIcon
            className="connectors-alert"
            message="MCP 未启用"
            description="当前进程以 --no-mcp 启动，连接器只会展示配置、不会真正连接。"
          />
        )}
        {running > 0 && (
          <Alert
            type="info"
            showIcon
            className="connectors-alert"
            message="有任务正在执行"
            description="为避免替换连接器影响正在运行的工具调用，执行结束后再修改配置。"
          />
        )}
        {remote && (
          <Alert
            type="info"
            showIcon
            className="connectors-alert"
            message="远程访问只读"
            description="连接器会启动本机命令，因此只能在运行 mini-agent 的电脑上修改。"
          />
        )}

        <div className="connectors-toolbar">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索连接器"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="connectors-search"
          />
          <span className="page-sub">
            {items.length ? `共 ${items.length} 个连接器` : ""}
          </span>
        </div>

        <Spin spinning={loading}>
          {filtered.length === 0 && !loading ? (
            <div className="connectors-empty">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={query ? "没有匹配的连接器" : "还没有连接器"}
              >
                {!query && (
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} disabled={locked}>
                    添加自定义连接器
                  </Button>
                )}
              </Empty>
            </div>
          ) : (
            <div className="connectors-grid">
              {filtered.map((c) => {
                const st = statusOf(c);
                return (
                  <div
                    key={c.name}
                    className={`connector-card ${c.enabled ? "" : "disabled"}`}
                    onClick={() => !locked && openEdit(c)}
                  >
                    <div className="connector-card-top">
                      <div className="connector-avatar">
                        {(c.display_name || c.name).slice(0, 1).toUpperCase()}
                      </div>
                      <div className="connector-card-actions" onClick={(e) => e.stopPropagation()}>
                        <Dropdown
                          trigger={["click"]}
                          menu={onCardMenu(c)}
                          disabled={locked}
                        >
                          <Button type="text" size="small" icon={<MoreOutlined />} />
                        </Dropdown>
                      </div>
                    </div>
                    <div className="connector-name" title={c.display_name}>
                      {c.display_name}
                    </div>
                    <div className="connector-desc" title={c.description || c.command}>
                      {c.description || c.command}
                    </div>
                    <div className="connector-meta">
                      <span className={`connector-state ${st.cls}`}>
                        {st.icon}
                        {st.label}
                      </span>
                      <Tooltip title={`已启用 ${c.tool_count} / 共 ${c.tool_total} 个工具`}>
                        <span className="connector-tools">
                          {c.tool_count}/{c.tool_total} 个工具
                        </span>
                      </Tooltip>
                    </div>
                    {c.enabled && !c.command_found && (
                      <div className="connector-warn">
                        <WarningOutlined /> 找不到命令 {c.command}
                      </div>
                    )}
                    {c.enabled && c.status === "failed" && c.error && (
                      <div className="connector-fail">
                        <Tooltip title={c.error}>
                          <div className="connector-error">{c.error}</div>
                        </Tooltip>
                        <div className="connector-fail-foot">
                          <span className="connector-hint">
                            {c.started_ms ? `启动 ${c.started_ms}ms` : ""}
                            {c.attempts > 1 ? ` · 尝试 ${c.attempts} 次` : ""}
                          </span>
                          <Button
                            size="small"
                            icon={<ReloadOutlined />}
                            loading={retrying === c.name}
                            disabled={locked}
                            onClick={(e) => {
                              e.stopPropagation();
                              void onRetry(c);
                            }}
                          >
                            重试
                          </Button>
                        </div>
                        {!!c.stderr_tail?.length && (
                          <pre className="connector-stderr" title={c.stderr_tail.join("\n")}>
                            {c.stderr_tail.join("\n")}
                          </pre>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Spin>
      </div>

      <Drawer
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        width={520}
        className="connector-editor"
        title={editing ? `编辑连接器 · ${editing.name}` : "自定义连接器"}
        extra={
          <Button
            type="primary"
            loading={saving}
            disabled={locked}
            onClick={() => void onSave()}
          >
            保存
          </Button>
        }
      >
        {locked && (
          <Alert
            type="info"
            showIcon
            className="connectors-alert"
            message={remote ? "远程访问只读" : running > 0 ? "有任务执行中" : "MCP 未启用"}
          />
        )}

        {presetHint && (
          <Alert
            type="info"
            showIcon
            className="connectors-alert"
            message="模板提示"
            description={presetHint}
          />
        )}

        <div className="connector-field">
          <label>标识</label>
          <Input
            value={draft.name}
            disabled={!!editing}
            placeholder="例如 github、my-tools"
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
          <span className="connector-hint">字母、数字、下划线或短横线；工具名会带上它做命名空间</span>
        </div>

        <div className="connector-field">
          <label>显示名称</label>
          <Input
            value={draft.display_name}
            placeholder="留空则使用标识"
            onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
          />
        </div>

        <div className="connector-field">
          <label>说明</label>
          <Input
            value={draft.description}
            placeholder="这个连接器能做什么"
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          />
        </div>

        <div className="connector-field">
          <label>传输方式</label>
          <Segmented
            value={draft.transport}
            options={[
              { label: "stdio（本地进程）", value: "stdio" },
              { label: "HTTP（远程服务）", value: "http" },
            ]}
            onChange={(v) =>
              setDraft({ ...draft, transport: v as "stdio" | "http" })
            }
          />
        </div>

        {draft.transport === "stdio" && (
          <>
            <div className="connector-field">
              <label>启动命令</label>
              <Input
                value={draft.command}
                placeholder="例如 npx、python、uvx"
                onChange={(e) => setDraft({ ...draft, command: e.target.value })}
              />
            </div>

            <div className="connector-field">
              <label>参数</label>
              <Input.TextArea
                value={draft.argsText}
                rows={4}
                placeholder={"每行一个参数，例如：\n-y\n@modelcontextprotocol/server-filesystem\nD:\\data"}
                onChange={(e) => setDraft({ ...draft, argsText: e.target.value })}
              />
            </div>

            <div className="connector-field">
              <label>工作目录</label>
              <Input
                value={draft.cwd}
                placeholder="可选，留空使用应用目录"
                onChange={(e) => setDraft({ ...draft, cwd: e.target.value })}
              />
            </div>

            <div className="connector-field">
              <label>环境变量</label>
              {draft.env.map((row, i) => (
                <div className="connector-env-row" key={i}>
                  <Input
                    value={row.k}
                    placeholder="KEY"
                    onChange={(e) => {
                      const env = [...draft.env];
                      env[i] = { ...row, k: e.target.value };
                      setDraft({ ...draft, env });
                    }}
                  />
                  <Input
                    value={row.v}
                    placeholder="VALUE"
                    onChange={(e) => {
                      const env = [...draft.env];
                      env[i] = { ...row, v: e.target.value };
                      setDraft({ ...draft, env });
                    }}
                  />
                  <Button
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() =>
                      setDraft({ ...draft, env: draft.env.filter((_, j) => j !== i) })
                    }
                  />
                </div>
              ))}
              <Button
                type="dashed"
                icon={<PlusOutlined />}
                onClick={() => setDraft({ ...draft, env: [...draft.env, { k: "", v: "" }] })}
              >
                添加变量
              </Button>
              <span className="connector-hint">
                已有变量的值以 *** 开头显示，表示已保存的密钥；不改动就会原样保留。
              </span>
            </div>
          </>
        )}

        {draft.transport === "http" && (
          <>
            <div className="connector-field">
              <label>服务地址</label>
              <Input
                value={draft.url}
                placeholder="https://example.com/mcp"
                onChange={(e) => setDraft({ ...draft, url: e.target.value })}
              />
              <span className="connector-hint">
                Streamable HTTP 端点；请填写 http(s):// 开头的完整地址。
              </span>
            </div>

            <div className="connector-field">
              <label>请求头</label>
              {draft.headers.map((row, i) => (
                <div className="connector-env-row" key={i}>
                  <Input
                    value={row.k}
                    placeholder="Authorization"
                    onChange={(e) => {
                      const headers = [...draft.headers];
                      headers[i] = { ...row, k: e.target.value };
                      setDraft({ ...draft, headers });
                    }}
                  />
                  <Input.Password
                    value={row.v}
                    placeholder="Bearer ***"
                    onChange={(e) => {
                      const headers = [...draft.headers];
                      headers[i] = { ...row, v: e.target.value };
                      setDraft({ ...draft, headers });
                    }}
                  />
                  <Button
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        headers: draft.headers.filter((_, j) => j !== i),
                      })
                    }
                  />
                </div>
              ))}
              <Button
                type="dashed"
                icon={<PlusOutlined />}
                onClick={() =>
                  setDraft({ ...draft, headers: [...draft.headers, { k: "", v: "" }] })
                }
              >
                添加请求头
              </Button>
              <span className="connector-hint">
                已保存的值以 *** 开头显示；不改动就会原样保留。
              </span>
            </div>
          </>
        )}

        {editing && (
          <div className="connector-field">
            <label>
              工具
              <span className="connector-hint">
                {" "}
                已启用{" "}
                {(editing.tools || []).filter(
                  (t) => !draft.disabledTools.includes(t.raw_name || t.name)
                ).length}
                /{(editing.tools || []).length}
              </span>
            </label>
            {(editing.tools || []).length ? (
              <div className="connector-tool-list">
                {(editing.tools || []).map((t) => {
                  const raw = t.raw_name || t.name;
                  return (
                    <div className="connector-tool-row" key={raw}>
                      <div className="connector-tool-info">
                        <code>{raw}</code>
                        {t.description && <span>{t.description}</span>}
                      </div>
                      <Switch
                        size="small"
                        checked={!draft.disabledTools.includes(raw)}
                        onChange={(v) =>
                          setDraft({
                            ...draft,
                            disabledTools: v
                              ? draft.disabledTools.filter((x) => x !== raw)
                              : [...draft.disabledTools, raw],
                          })
                        }
                      />
                    </div>
                  );
                })}
              </div>
            ) : (
              <span className="connector-hint">
                连接成功后这里会列出服务端工具，可逐个关闭。
              </span>
            )}
            <span className="connector-hint">
              关闭的工具不会注册给模型（对所有会话生效），仍受各会话的连接器勾选约束。
            </span>
          </div>
        )}

        <div className="connector-field connector-field-inline">
          <label>超时（秒）</label>
          <InputNumber
            min={1}
            max={600}
            value={draft.timeout}
            onChange={(v) => setDraft({ ...draft, timeout: Number(v) || 30 })}
          />
        </div>

        <div className="connector-field connector-field-inline">
          <label>启用</label>
          <Switch
            checked={draft.enabled}
            onChange={(v) => setDraft({ ...draft, enabled: v })}
          />
        </div>

        <div className="connector-field connector-field-inline">
          <label>危险操作确认</label>
          <Switch
            checked={draft.dangerous}
            onChange={(v) => setDraft({ ...draft, dangerous: v })}
          />
        </div>
        <span className="connector-hint">
          开启后，模型调用该连接器的工具前需要你确认；外部写入/命令类建议保持开启。
        </span>

        {nodeMissing && (
          <Alert
            type="warning"
            showIcon
            className="connectors-alert"
            message="未检测到 npx"
            description={
              <span>
                这类连接器需要 Node.js。安装后重启应用再试：
                <a href="https://nodejs.org/zh-cn" target="_blank" rel="noreferrer">
                  nodejs.org/zh-cn
                </a>
              </span>
            }
          />
        )}

        <div className="connector-test-row">
          <Button icon={<ThunderboltOutlined />} loading={testing} onClick={() => void onTest()}>
            测试连接
          </Button>
          <span className="connector-hint">起一个临时进程握手、列工具，然后立即关闭，不会保存</span>
        </div>

        {testResult && (
          <div className={`connector-test-result ${testResult.ok ? "ok" : "err"}`}>
            {testResult.ok ? (
              <>
                <div className="connector-test-head">
                  <CheckCircleFilled /> 连接成功，发现 {testResult.tools?.length || 0} 个工具
                  {typeof testResult.server_info?.name === "string" && (
                    <span className="connector-test-server">
                      {" "}
                      · {String(testResult.server_info.name)}{" "}
                      {typeof testResult.server_info.version === "string"
                        ? testResult.server_info.version
                        : ""}
                    </span>
                  )}
                </div>
                <ul className="connector-test-tools">
                  {(testResult.tools || []).map((t) => (
                    <li key={t.name}>
                      <code>{t.name}</code>
                      {t.description && <span>{t.description}</span>}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <>
                <div className="connector-test-head">
                  <CloseCircleFilled /> 连接失败
                </div>
                <div className="connector-test-error">{testResult.error || "未知错误"}</div>
                {!!testResult.stderr?.length && (
                  <pre className="connector-test-stderr">{testResult.stderr.join("\n")}</pre>
                )}
              </>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={!!deleting}
        title="删除连接器"
        okText="删除"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        onOk={() => void onDelete()}
        onCancel={() => setDeleting(null)}
      >
        确定删除 <b>{deleting?.display_name}</b>？它的工具会立即从会话中移除，配置文件也会一并删除。
      </Modal>
    </div>
  );
}
