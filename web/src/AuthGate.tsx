/**
 * 登录门禁：应用启动的第一层。
 *
 * 三种状态：
 *   1. 未登录        → LoginPage
 *   2. 必须改初始密码 → ForceChangePassword（后端也会 403 拦截业务接口）
 *   3. 已登录        → App
 *
 * 401 统一处理：api.ts 的任意请求收到 401 都会回调这里，直接切回登录页，
 * 避免各调用方各写一遍「登录过期」逻辑。
 */
import { useCallback, useEffect, useState } from "react";
import { Spin } from "antd";
import App from "./App";
import LoginPage from "./components/LoginPage";
import ForceChangePassword from "./components/ForceChangePassword";
import { fetchAuthMe, logout, setUnauthorizedHandler } from "./api";
import type { User } from "./types";

export default function AuthGate() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [loginEnabled, setLoginEnabled] = useState(true);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let alive = true;
    fetchAuthMe()
      .then((r) => {
        if (!alive) return;
        setUser(r.user);
        setLoginEnabled(r.login_enabled);
      })
      .catch(() => {
        if (alive) setUser(null);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } catch {
      // 会话可能已经过期，登出失败不需要打扰用户
    }
    setUser(null);
  }, []);

  if (loading) {
    return (
      <div className="auth-loading">
        <Spin size="large" />
      </div>
    );
  }

  if (!user) return <LoginPage onSuccess={setUser} />;

  if (user.must_change_password) {
    return (
      <ForceChangePassword user={user} onDone={setUser} onLogout={handleLogout} />
    );
  }

  return (
    <App user={user} loginEnabled={loginEnabled} onLogout={handleLogout} />
  );
}
