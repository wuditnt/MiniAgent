/**
 * 「模型配置」面板：多服务商 / 多模型配置，一键切换，内置国内主流模板，支持连接测试。
 * 外层是已配置列表，内层是「新增 / 编辑」表单。API Key 全程只存本机。
 * 本身不是弹窗 —— 由「系统配置」弹窗的页签承载。
 */
import { useEffect, useMemo, useState } from "react";
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  AutoComplete,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
} from "antd";
import {
  activateModel,
  createModel,
  deleteModel,
  fetchModels,
  testModel,
  testModelRaw,
  updateModel,
} from "../api";
import type { ModelProvider, ModelsInfo, TestResult } from "../types";

interface Props {
  /** 配置变更后通知外层重新拉 /api/state（顶栏模型名要跟着变） */
  onChanged: () => void;
}

const DEFAULT_PROVIDER = "deepseek";

export default function ModelSettingsPanel({ onChanged }: Props) {
  const [info, setInfo] = useState<ModelsInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [tests, setTests] = useState<Record<string, TestResult | "loading">>({});

  // 内层表单
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ModelProvider | null>(null);
  const [provider, setProvider] = useState(DEFAULT_PROVIDER);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);
  const [kwTest, setKwTest] = useState<TestResult | "loading" | null>(null);
  const [formError, setFormError] = useState("");

  const reload = () => {
    setLoading(true);
    fetchModels()
      .then(setInfo)
      .catch(() => setInfo(null))
      .finally(() => setLoading(false));
  };

  // 作为「系统配置」的一个页签：挂载即拉取
  useEffect(() => {
    reload();
    setTests({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const templates = info?.templates || {};
  const tpl = templates[provider];
  const providers = info?.providers || [];

  const templateOptions = useMemo(
    () =>
      Object.entries(templates).map(([key, t]) => ({
        value: key,
        label: t.name,
      })),
    [templates]
  );

  const modelOptions = (tpl?.models || []).map((m) => ({ value: m }));

  // ---------------------------------------------------------- 列表操作

  const doTest = async (id: string) => {
    setTests((t) => ({ ...t, [id]: "loading" }));
    try {
      const r = await testModel(id);
      setTests((t) => ({ ...t, [id]: r }));
    } catch (e) {
      setTests((t) => ({ ...t, [id]: { ok: false, error: String(e) } }));
    }
  };

  const doActivate = async (id: string) => {
    await activateModel(id);
    reload();
    onChanged();
  };

  const doDelete = async (id: string) => {
    try {
      await deleteModel(id);
      reload();
    } catch (e) {
      setTests((t) => ({ ...t, [id]: { ok: false, error: String(e) } }));
    }
  };

  // ---------------------------------------------------------- 表单

  const openNew = () => {
    const first = DEFAULT_PROVIDER;
    const t = templates[first];
    setEditing(null);
    setProvider(first);
    setName(t?.name || "");
    setBaseUrl(t?.base_url || "");
    setApiKey("");
    setModel(t?.models?.[0] || "");
    setKwTest(null);
    setFormError("");
    setFormOpen(true);
  };

  const openEdit = (p: ModelProvider) => {
    setEditing(p);
    setProvider(p.provider);
    setName(p.name);
    setBaseUrl(p.base_url);
    setApiKey(""); // 留空 = 不修改
    setModel(p.model);
    setKwTest(null);
    setFormError("");
    setFormOpen(true);
  };

  /** 切换模板：自动填地址与首个模型名（仅新建时） */
  const onProviderChange = (key: string) => {
    setProvider(key);
    if (!editing) {
      const t = templates[key];
      setBaseUrl(t?.base_url || "");
      setName(t?.name || "");
      setModel(t?.models?.[0] || "");
    }
  };

  const doKwTest = async () => {
    if (!baseUrl.trim()) {
      setKwTest({ ok: false, error: "请先填写接口地址" });
      return;
    }
    // 编辑态且未重填 key → 用已存配置测
    if (editing && !apiKey) {
      await doTestInline(editing.id);
      return;
    }
    setKwTest("loading");
    try {
      setKwTest(await testModelRaw(baseUrl.trim(), apiKey));
    } catch (e) {
      setKwTest({ ok: false, error: String(e) });
    }
  };

  const doTestInline = async (id: string) => {
    setKwTest("loading");
    try {
      setKwTest(await testModel(id));
    } catch (e) {
      setKwTest({ ok: false, error: String(e) });
    }
  };

  const save = async () => {
    setFormError("");
    if (!baseUrl.trim() || !model.trim()) {
      setFormError("接口地址与模型名必填");
      return;
    }
    if (!editing && !apiKey && !tpl?.no_key) {
      setFormError("请填写 API Key");
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        await updateModel(editing.id, {
          name,
          base_url: baseUrl.trim(),
          model: model.trim(),
          ...(apiKey ? { api_key: apiKey } : {}),
        });
      } else {
        await createModel({
          provider,
          name,
          base_url: baseUrl.trim(),
          api_key: apiKey,
          model: model.trim(),
        });
      }
      setFormOpen(false);
      reload();
      onChanged();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const renderResult = (r: TestResult | "loading" | null) => {
    if (!r) return null;
    if (r === "loading") return <Spin size="small" />;
    return r.ok ? (
      <span className="ms-ok">
        <CheckCircleOutlined /> 连接正常{r.models?.length ? `（${r.models.length} 个模型）` : ""}
        {r.note ? ` · ${r.note}` : ""}
      </span>
    ) : (
      <span className="ms-err">
        <CloseCircleOutlined /> {r.error}
      </span>
    );
  };

  return (
    <>
      <div className="ms-panel">
        <Spin spinning={loading}>
          {providers.length === 0 ? (
            <Empty description="暂无模型配置" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <div className="ms-list">
              {providers.map((p) => (
                <div key={p.id} className={`ms-card ${p.is_active ? "active" : ""}`}>
                  <div className="ms-card-main">
                    <div className="ms-card-title">
                      {p.name}
                      {p.is_active && (
                        <Tag color="blue" style={{ marginLeft: 8 }}>
                          当前
                        </Tag>
                      )}
                    </div>
                    <div className="ms-card-sub">
                      {p.model} · {p.base_url}
                    </div>
                    <div className="ms-card-key">
                      {p.has_key ? p.api_key_masked : "无需 API Key"}
                    </div>
                    <div className="ms-card-result">{renderResult(tests[p.id])}</div>
                  </div>
                  <Space direction="vertical" size={6} className="ms-card-actions">
                    <Button
                      size="small"
                      icon={<ApiOutlined />}
                      onClick={() => doTest(p.id)}
                      loading={tests[p.id] === "loading"}
                    >
                      连接测试
                    </Button>
                    {!p.is_active && (
                      <Button
                        size="small"
                        type="primary"
                        icon={<ThunderboltOutlined />}
                        onClick={() => doActivate(p.id)}
                      >
                        设为当前
                      </Button>
                    )}
                    <Space size={4}>
                      <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(p)}>
                        编辑
                      </Button>
                      <Popconfirm
                        title="删除这份配置？"
                        okText="删除"
                        cancelText="取消"
                        onConfirm={() => doDelete(p.id)}
                      >
                        <Button size="small" danger icon={<DeleteOutlined />} />
                      </Popconfirm>
                    </Space>
                  </Space>
                </div>
              ))}
            </div>
          )}
          <Button block icon={<PlusOutlined />} onClick={openNew} style={{ marginTop: 12 }}>
            添加模型配置
          </Button>
        </Spin>
      </div>

      <Modal
        open={formOpen}
        title={editing ? "编辑模型配置" : "添加模型配置"}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={save}
        onCancel={() => setFormOpen(false)}
        destroyOnClose
      >
        <Form layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item label="服务商模板" style={{ marginBottom: 12 }}>
            <Select
              value={provider}
              onChange={onProviderChange}
              options={templateOptions}
              disabled={!!editing}
            />
          </Form.Item>
          <Form.Item label="显示名称" style={{ marginBottom: 12 }}>
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} />
          </Form.Item>
          <Form.Item label="接口地址 (Base URL)" required style={{ marginBottom: 12 }}>
            <Input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.deepseek.com/v1"
            />
          </Form.Item>
          <Form.Item
            label="API Key"
            style={{ marginBottom: 12 }}
            extra={
              editing?.has_key
                ? `留空表示不修改（当前 ${editing.api_key_masked}）`
                : tpl?.no_key
                ? "本地服务可留空"
                : "仅保存在本机，不会上传"
            }
          >
            <Input.Password
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={tpl?.no_key ? "本地服务无需 Key" : "sk-..."}
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item label="模型名" required style={{ marginBottom: 12 }}>
            <AutoComplete
              value={model}
              onChange={(v) => setModel(v)}
              options={modelOptions}
              placeholder="如 deepseek-chat"
            />
          </Form.Item>
        </Form>
        <div className="ms-form-foot">
          <Button icon={<ApiOutlined />} onClick={doKwTest} loading={kwTest === "loading"}>
            连接测试
          </Button>
          <span className="ms-form-result">{renderResult(kwTest)}</span>
        </div>
        {formError && <div className="ws-modal-error">{formError}</div>}
      </Modal>
    </>
  );
}
