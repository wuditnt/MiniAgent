/**
 * 系统配置 → 网络（仅桌面端渲染）。
 *
 * 局域网开关属于「桌面壳的启动参数」而不是后端配置：
 * 值存在 desktop-config.json（main 进程经 IPC 读写），保存后必须重启应用
 * 才会以新的 --host 重新绑定端口，所以这里只负责展示 + 触发重启。
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Spin, Switch } from "antd";
import { fetchHealth } from "../api";
import type { HealthInfo } from "../types";

export default function NetworkPanel() {
  const bridge = window.__DESKTOP__;
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [lanEnabled, setLanEnabled] = useState(false);
  const [restartRequired, setRestartRequired] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    if (!bridge) return;
    setError("");
    Promise.all([fetchHealth(), bridge.getConfig()])
      .then(([h, cfg]) => {
        setHealth(h);
        setLanEnabled(cfg.lan_enabled);
        setRestartRequired(cfg.lan_enabled !== h.lan_enabled);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [bridge]);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = async (value: boolean) => {
    if (!bridge) return;
    setSaving(true);
    setError("");
    try {
      const r = await bridge.setLanEnabled(value);
      setLanEnabled(r.lan_enabled);
      setRestartRequired(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  if (!bridge) return null;

  const wildcard = health?.host === "0.0.0.0" || health?.host === "::";
  const lanUrls = health?.lan_urls ?? [];

  return (
    <Spin spinning={saving}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

      <div className="gs-group-title">局域网访问</div>
      <div className="gs-section">
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            padding: "8px 0",
          }}
        >
          <div>
            <div className="sandbox-opt-name">允许局域网访问（手机 / 平板）</div>
            <div className="sandbox-opt-desc">
              开启后，同一网络下的设备可以通过手机浏览器访问本应用；
              关闭时只监听 127.0.0.1，仅本机可用
            </div>
          </div>
          <Switch checked={lanEnabled} loading={saving} onChange={toggle} />
        </div>

        {restartRequired && (
          <Alert
            type="info"
            showIcon
            message="已保存，重启应用后生效"
            style={{ marginTop: 8 }}
            action={
              <Button size="small" type="primary" onClick={() => void bridge.restart()}>
                立即重启
              </Button>
            }
          />
        )}
      </div>

      <div className="gs-group-title">当前监听</div>
      <div className="gs-section">
        <div style={{ padding: "8px 0" }}>
          <div className="sandbox-opt-name">
            {health ? `${health.host}:${health.port}` : "读取中 …"}
            {health && (
              <span style={{ marginLeft: 8, color: "#888", fontWeight: 400 }}>
                {wildcard ? "局域网可访问" : "仅本机可访问"}
              </span>
            )}
          </div>
          {wildcard && lanUrls.length > 0 && (
            <div className="sandbox-opt-desc" style={{ marginTop: 6 }}>
              局域网地址：
              {lanUrls.map((u) => (
                <div key={u} style={{ fontFamily: "monospace" }}>
                  {u}
                </div>
              ))}
            </div>
          )}
          {wildcard && lanUrls.length === 0 && (
            <div className="sandbox-opt-desc" style={{ marginTop: 6 }}>
              未检测到局域网 IP，请检查网络连接
            </div>
          )}
        </div>
        <Alert
          type="warning"
          showIcon
          message="开启局域网后，同一网络内的设备可访问并操作本机文件，请只在可信网络中使用。"
          style={{ marginTop: 8 }}
        />
        <div className="gs-note" style={{ marginTop: 8 }}>
          首次开启时 Windows 可能询问防火墙权限，请选择「允许专用网络」；
          若误点取消，手机将无法访问（可在 Windows 防火墙设置里重新放行）。
        </div>
      </div>

      <div className="gs-group-title">数据与日志</div>
      <div className="gs-section">
        <div style={{ padding: "8px 0" }}>
          <div className="sandbox-opt-name">数据目录</div>
          <div className="sandbox-opt-desc" style={{ fontFamily: "monospace" }}>
            {health?.data_dir || "读取中 …"}
          </div>
        </div>
      </div>
    </Spin>
  );
}
