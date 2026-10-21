/**
 * 首次登录 / 管理员重置密码后的强制改密页。
 *
 * 不可跳过：后端对所有业务接口都返回 403 MUST_CHANGE_PASSWORD，
 * 这里只是把这层状态可视化，并给一个唯一的出口。
 */
import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { changePassword } from "../api";
import type { User } from "../types";

export default function ForceChangePassword({
  user,
  onDone,
  onLogout,
}: {
  user: User;
  onDone: (u: User) => void;
  onLogout: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (loading) return;
    if (!current || !next) {
      setError("请填写当前密码与新密码");
      return;
    }
    if (next !== confirm) {
      setError("两次输入的新密码不一致");
      return;
    }
    if (next.length < 8) {
      setError("新密码至少 8 个字符");
      return;
    }
    setLoading(true);
    setError("");
    try {
      onDone(await changePassword(current, next));
    } catch (e) {
      setError(e instanceof Error ? e.message : "修改失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <span className="login-logo">修改初始密码</span>
          <span className="login-sub">
            {user.display_name || user.username}，首次登录必须先设置新密码
          </span>
        </div>

        <Form layout="vertical" onFinish={submit}>
          <Form.Item label="当前密码">
            <Input.Password
              size="large"
              value={current}
              autoFocus
              autoComplete="current-password"
              onChange={(e) => setCurrent(e.target.value)}
            />
          </Form.Item>
          <Form.Item label="新密码">
            <Input.Password
              size="large"
              value={next}
              autoComplete="new-password"
              placeholder="至少 8 个字符"
              onChange={(e) => setNext(e.target.value)}
            />
          </Form.Item>
          <Form.Item label="确认新密码">
            <Input.Password
              size="large"
              value={confirm}
              autoComplete="new-password"
              onChange={(e) => setConfirm(e.target.value)}
            />
          </Form.Item>

          {error && (
            <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
          )}

          <Button type="primary" size="large" block loading={loading} htmlType="submit">
            设置新密码并继续
          </Button>
          <Button type="text" block style={{ marginTop: 8 }} onClick={onLogout}>
            退出登录
          </Button>
        </Form>
      </div>
    </div>
  );
}
