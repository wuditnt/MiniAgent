/**
 * 登录页：整屏居中卡片，未登录时唯一可见的界面。
 *
 * 为什么不做「先渲染 App 再弹登录框」：App 挂载就会打一堆 /api/*，
 * 未登录时全是 401 噪音；先把登录挡在前面，挂载即已登录，逻辑最简单。
 */
import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { login } from "../api";
import type { User } from "../types";

/** 只记用户名，绝不记密码 */
const REMEMBER_KEY = "miniagent.username";

export default function LoginPage({ onSuccess }: { onSuccess: (u: User) => void }) {
  const [username, setUsername] = useState(() => localStorage.getItem(REMEMBER_KEY) || "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (loading) return;
    const name = username.trim();
    if (!name || !password) {
      setError("请输入用户名和密码");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const user = await login(name, password);
      localStorage.setItem(REMEMBER_KEY, user.username);
      onSuccess(user);
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <span className="login-logo">mini-agent</span>
          <span className="login-sub">本地智能体控制台</span>
        </div>

        <Form layout="vertical" onFinish={submit}>
          <Form.Item label="用户名">
            <Input
              size="large"
              prefix={<UserOutlined />}
              value={username}
              autoFocus
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
            />
          </Form.Item>
          <Form.Item label="密码">
            <Input.Password
              size="large"
              prefix={<LockOutlined />}
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Form.Item>

          {error && (
            <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
          )}

          <Button type="primary" size="large" block loading={loading} htmlType="submit">
            登录
          </Button>
        </Form>

        <div className="login-hint">
          首次使用：默认账号 <b>admin</b> / <b>admin123</b>，登录后必须修改密码。
          忘记密码可在终端执行 <code>--reset-password</code>。
        </div>
      </div>
    </div>
  );
}
