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
