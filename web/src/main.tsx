import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AuthGate from "./AuthGate";
import "./global.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <AuthGate />
    </ConfigProvider>
  </React.StrictMode>
);

// PWA：只有生产构建才注册 Service Worker（开发时它会缓存住模块，破坏热更新）。
// 浏览器要求安全上下文（https 或 localhost）；局域网 http 下注册必然失败，
// 这正是预期行为，所以静默忽略 —— 仍可通过「添加到主屏幕」使用独立窗口。
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* 非安全上下文 / 浏览器不支持：不影响正常使用 */
    });
  });
}
