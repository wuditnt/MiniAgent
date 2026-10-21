/**
 * 修改自己的密码（顶栏「⋯ → 修改密码」）。
 * 后端改密后会保留当前会话、踢掉该账号的其它会话。
 */
import { useEffect, useState } from "react";
import { Alert, Input, Modal } from "antd";
import { changePassword } from "../api";

export default function ChangePasswordModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (open) {
      setCurrent("");
      setNext("");
      setConfirm("");
      setError("");
    }
  }, [open]);

  const submit = async () => {
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
      await changePassword(current, next);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "修改失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      open={open}
      title="修改密码"
      okText="保存"
      cancelText="取消"
      confirmLoading={loading}
      onOk={submit}
      onCancel={onClose}
      destroyOnClose
    >
      <div className="form-stack">
        <label>当前密码</label>
        <Input.Password
          value={current}
          autoComplete="current-password"
          onChange={(e) => setCurrent(e.target.value)}
        />
        <label>新密码</label>
        <Input.Password
          value={next}
          autoComplete="new-password"
          placeholder="至少 8 个字符"
          onChange={(e) => setNext(e.target.value)}
        />
        <label>确认新密码</label>
        <Input.Password
          value={confirm}
          autoComplete="new-password"
          onChange={(e) => setConfirm(e.target.value)}
        />
        {error && <Alert type="error" showIcon message={error} />}
        <div className="form-tip">修改后，该账号在其它设备上的登录会立即失效。</div>
      </div>
    </Modal>
  );
}
