/**
 * 连接页（占满主区，包含原本右侧的工作区文件区）：
 * 把外部 MCP server 接入 mini-agent。
 *
 * 第一版只支持 stdio（本机命令），数据来自 /api/connectors。
 * 写操作仅本机 + 超级管理员可用，远程访问时只读。
 */
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  ApiOutlined,
  ArrowLeftOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  EditOutlined,
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
  Spin,
  Switch,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { MenuProps } from "antd";
import {
  deleteConnector,
  fetchConnectors,
  saveConnector,
  testConnector,
  toggleConnector,
} from "../api";
import type {
  ConnectorInfo,
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
  command: string;
  argsText: string;
  cwd: string;
  timeout: number;
  enabled: boolean;
  dangerous: boolean;
  env: EnvRow[];
}

const NAME_RE = /^[A-Za-z0-9_-]{1,40}$/;

function draftFrom(c?: ConnectorInfo): Draft {
  return {
    name: c?.name || "",
    display_name: c?.display_name || "",
    description: c?.description || "",
    command: c?.command || "",
    argsText: (c?.args || []).join("\n"),
    cwd: c?.cwd || "",
    timeout: c?.timeout || 30,
    enabled: c?.enabled ?? true,
    dangerous: c?.dangerous ?? true,
    env: Object.entries(c?.env || {}).map(([k, v]) => ({ k, v })),
  };
}

function specFrom(draft: Draft): ConnectorSpec {
  const env: Record<string, string> = {};
  for (const row of draft.env) {
    const key = row.k.trim();
    if (key) env[key] = row.v;
  }
  return {
    name: draft.name.trim(),
    display_name: draft.display_name.trim() || draft.name.trim(),
    description: draft.description.trim(),
    transport: "stdio",
    command: draft.command.trim(),
    args: draft.argsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean),
    cwd: draft.cwd.trim(),
    env,
    timeout: draft.timeout,
    enabled: draft.enabled,
    dangerous: draft.dangerous,
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
    setEditorOpen(true);
  }

  function openEdit(c: ConnectorInfo) {
    setEditing(c);
    setDraft(draftFrom(c));
    setTestResult(null);
    setEditorOpen(true);
  }

  async function onTest() {
    const spec = specFrom(draft);
    if (!spec.name || !NAME_RE.test(spec.name)) {
      message.warning("请先填写合法的连接器标识");
      return;
    }
    if (!spec.command) {
      message.warning("请先填写启动命令");
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
    if (!spec.command) {
      message.warning("启动命令不能为空");
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
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} disabled={locked}>
            自定义连接器
          </Button>
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
                      <span className="connector-tools">{c.tool_count} 个工具</span>
                    </div>
                    {c.enabled && !c.command_found && (
                      <div className="connector-warn">
                        <WarningOutlined /> 找不到命令 {c.command}
                      </div>
                    )}
                    {c.enabled && c.status === "failed" && c.error && (
                      <Tooltip title={c.error}>
                        <div className="connector-error">{c.error}</div>
                      </Tooltip>
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
        </div>

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
