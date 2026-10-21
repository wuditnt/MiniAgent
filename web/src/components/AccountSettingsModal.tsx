/**
 * 账号管理（仅超级管理员可见）。
 *
 * 为什么独立一个弹窗而不是塞进「系统配置」：系统配置管的是运行参数，
 * 账号是权限数据；两者混在一起会让「恢复默认」这类操作变得危险。
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { PlusOutlined } from "@ant-design/icons";
import {
  createUser,
  deleteUser,
  fetchUsers,
  logoutAllSessions,
  updateUser,
} from "../api";
import type { User, UserRole } from "../types";

const ROLE_LABEL: Record<UserRole, string> = {
  superadmin: "超级管理员",
  user: "普通用户",
};

function fmtTime(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export default function AccountSettingsModal({
  open,
  current,
  onClose,
}: {
  open: boolean;
  current: User;
  onClose: () => void;
}) {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [resetFor, setResetFor] = useState<User | null>(null);
  const [resetPwd, setResetPwd] = useState("");
  const [resetErr, setResetErr] = useState("");
  const [form] = Form.useForm();

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setUsers(await fetchUsers());
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载账号失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  const patch = async (
    u: User,
    payload: Parameters<typeof updateUser>[1],
    tip = "已保存"
  ) => {
    try {
      await updateUser(u.id, payload);
      message.success(tip);
      await reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
      await reload();   // 后端拒绝时把界面拉回真实状态（如最后一个超管不能降级）
    }
  };

  const submitCreate = async () => {
    const v = await form.validateFields();
    setCreating(true);
    try {
      await createUser({
        username: v.username.trim(),
        password: v.password,
        display_name: (v.display_name || "").trim(),
        role: v.role as UserRole,
        must_change_password: v.must_change_password !== false,
      });
      message.success("账号已创建，首次登录需修改密码");
      setCreateOpen(false);
      form.resetFields();
      await reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const submitReset = async () => {
    if (!resetFor) return;
    if (resetPwd.length < 8) {
      setResetErr("密码至少 8 个字符");
      return;
    }
    try {
      await updateUser(resetFor.id, { password: resetPwd });
      message.success("密码已重置，该账号的其它登录已失效");
      setResetFor(null);
      setResetPwd("");
      await reload();
    } catch (e) {
      setResetErr(e instanceof Error ? e.message : "重置失败");
    }
  };

  const columns: ColumnsType<User> = [
    {
      title: "用户名",
      dataIndex: "username",
      render: (username: string, u) => (
        <span className="acct-name">
          {username}
          {u.id === current.id && <Tag color="blue">当前</Tag>}
        </span>
      ),
    },
    {
      title: "显示名",
      dataIndex: "display_name",
      render: (name: string, u) =>
        u.id === current.id ? (
          name
        ) : (
          <Input
            size="small"
            defaultValue={name}
            maxLength={40}
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v !== name) void patch(u, { display_name: v });
            }}
          />
        ),
    },
    {
      title: "角色",
      dataIndex: "role",
      width: 150,
      render: (role: UserRole, u) =>
        u.id === current.id ? (
          <Tag color="geekblue">{ROLE_LABEL[role]}</Tag>
        ) : (
          <Select
            size="small"
            value={role}
            style={{ width: 120 }}
            options={[
              { value: "user", label: ROLE_LABEL.user },
              { value: "superadmin", label: ROLE_LABEL.superadmin },
            ]}
            onChange={(v) => void patch(u, { role: v as UserRole })}
          />
        ),
    },
    {
      title: "状态",
      dataIndex: "disabled",
      width: 100,
      render: (disabled: boolean, u) => (
        <Switch
          size="small"
          checked={!disabled}
          checkedChildren="启用"
          unCheckedChildren="禁用"
          disabled={u.id === current.id}
          onChange={(checked) => void patch(u, { disabled: !checked })}
        />
      ),
    },
    {
      title: "最近登录",
      dataIndex: "last_login_at",
      width: 180,
      render: (v: string | null) => <span className="acct-time">{fmtTime(v)}</span>,
    },
    {
      title: "操作",
      width: 210,
      render: (_, u) => (
        <div className="acct-actions">
          {u.id !== current.id && (
            <Button
              type="link"
              size="small"
              onClick={() => {
                setResetFor(u);
                setResetPwd("");
                setResetErr("");
              }}
            >
              重置密码
            </Button>
          )}
          {u.id !== current.id && (
            <Popconfirm
              title="强制该账号下线？"
              description="会删除它的全部登录会话，重新登录即可继续使用。"
              okText="下线"
              cancelText="取消"
              onConfirm={async () => {
                try {
                  await logoutAllSessions(u.id);
                  message.success("已强制下线");
                } catch (e) {
                  message.error(e instanceof Error ? e.message : "操作失败");
                }
              }}
            >
              <Button type="link" size="small">
                强制下线
              </Button>
            </Popconfirm>
          )}
          {u.id !== current.id && (
            <Popconfirm
              title={`删除账号 ${u.username}？`}
              description="删除后不可恢复。"
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={async () => {
                try {
                  await deleteUser(u.id);
                  message.success("已删除");
                  await reload();
                } catch (e) {
                  message.error(e instanceof Error ? e.message : "删除失败");
                }
              }}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          )}
        </div>
      ),
    },
  ];

  return (
    <Modal
      open={open}
      title="账号管理"
      width={980}
      footer={null}
      onCancel={onClose}
      destroyOnClose
    >
      <div className="acct-toolbar">
        <div className="acct-tip">
          所有账号共用同一份会话与工作区；超级管理员可创建账号、重置密码、禁用账号。
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建账号
        </Button>
      </div>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={users}
        columns={columns}
        pagination={false}
      />

      <Modal
        open={createOpen}
        title="新建账号"
        okText="创建"
        cancelText="取消"
        confirmLoading={creating}
        onOk={submitCreate}
        onCancel={() => setCreateOpen(false)}
        destroyOnClose
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ role: "user", must_change_password: true }}
        >
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: "请输入用户名" },
              {
                pattern: /^[A-Za-z0-9_\-\u4e00-\u9fa5]{3,32}$/,
                message: "3–32 位字母、数字、下划线、连字符或中文",
              },
            ]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名（可选）">
            <Input maxLength={40} />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[
              { required: true, message: "请输入初始密码" },
              { min: 8, message: "至少 8 个字符" },
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select
              options={[
                { value: "user", label: ROLE_LABEL.user },
                { value: "superadmin", label: ROLE_LABEL.superadmin },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="must_change_password"
            label="首次登录必须修改密码"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={resetFor !== null}
        title={`重置 ${resetFor?.username ?? ""} 的密码`}
        okText="重置"
        cancelText="取消"
        onOk={submitReset}
        onCancel={() => setResetFor(null)}
        destroyOnClose
      >
        <div className="form-stack">
          <label>新密码</label>
          <Input.Password
            value={resetPwd}
            autoComplete="new-password"
            placeholder="至少 8 个字符"
            onChange={(e) => {
              setResetPwd(e.target.value);
              setResetErr("");
            }}
          />
          {resetErr && <Alert type="error" showIcon message={resetErr} />}
          <div className="form-tip">
            重置后该账号的所有登录会话会立即失效，并需要在下次登录时修改密码。
          </div>
        </div>
      </Modal>
    </Modal>
  );
}
