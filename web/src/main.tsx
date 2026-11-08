import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import AuthGate from "./AuthGate";
import { ThemeProvider, useTheme } from "./theme";
import "./global.css";
import "./theme-dark.css";

/** 把当前主题喂给 antd：组件库自带深色算法，无需逐个组件改样式 */
function ThemedApp() {
  const { theme } = useTheme();
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme === "dark" ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
      }}
    >
      <AuthGate />
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <ThemedApp />
    </ThemeProvider>
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
