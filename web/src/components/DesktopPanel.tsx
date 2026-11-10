/**
 * 系统配置 → 桌面（仅桌面端渲染）。
 *
 * 关闭按钮的行为属于桌面壳自己的偏好：存在 desktop-config.json，由 main 进程
 * 读写；这里只做展示与切换，改完立即生效，不需要重启应用。
 */
import { useEffect, useState } from "react";
import { Alert, Radio, Spin } from "antd";
import type { DesktopCloseAction } from "../types";

const OPTIONS: { value: DesktopCloseAction; label: string; desc: string }[] = [
  {
    value: "tray",
    label: "最小化到托盘",
    desc: "窗口收进系统托盘，点击托盘图标恢复；右键图标可打开主界面或退出。",
  },
  {
    value: "quit",
    label: "退出程序",
    desc: "关闭窗口即结束应用，后台任务会随之中断。",
  },
  {
    value: "ask",
    label: "每次都询问",
    desc: "每次点击关闭都弹窗确认，不记住选择。",
  },
];

export default function DesktopPanel() {
  const bridge = typeof window !== "undefined" ? window.__DESKTOP__ : undefined;
  const [action, setAction] = useState<DesktopCloseAction>("ask");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!bridge) return;
    let alive = true;
    bridge
      .getConfig()
      .then((cfg) => {
        if (alive) setAction(cfg.close_action);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [bridge]);

  if (!bridge) return null;

  const change = async (value: DesktopCloseAction) => {
    setSaving(true);
    setError("");
    try {
      const cfg = await bridge.setCloseAction(value);
      setAction(cfg.close_action);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Spin spinning={loading || saving}>
      {error && (
        <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />
      )}
      <div className="gs-group-title">关闭窗口</div>
      <div className="gs-section">
        <div className="sandbox-opt-name">点击窗口关闭按钮时</div>
        <Radio.Group
          value={action}
          onChange={(e) => void change(e.target.value as DesktopCloseAction)}
          style={{ marginTop: 10, display: "block" }}
        >
          {OPTIONS.map((o) => (
            <div key={o.value} style={{ marginBottom: 12 }}>
              <Radio value={o.value}>{o.label}</Radio>
              <div className="sandbox-opt-desc" style={{ marginLeft: 26 }}>
                {o.desc}
              </div>
            </div>
          ))}
        </Radio.Group>
        <div className="gs-note">
          修改后立即生效。收进托盘后，用托盘菜单的「退出」才会真正结束应用。
        </div>
      </div>
    </Spin>
  );
}
