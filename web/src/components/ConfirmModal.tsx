/**
 * 危险操作确认弹窗：confirm_request 事件触发。
 * 只有两个出口（允许/拒绝），关闭按钮与 Esc 都映射为「拒绝」，防止误放行。
 */
import { Modal, Tag } from "antd";
import type { ConfirmInfo } from "../types";

interface Props {
  info: ConfirmInfo | null;
  onAnswer: (approve: boolean) => void;
}

export default function ConfirmModal({ info, onAnswer }: Props) {
  return (
    <Modal
      open={!!info}
      title="危险操作确认"
      okText="允许执行"
      cancelText="拒绝"
      okButtonProps={{ danger: true }}
      closable={false}
      maskClosable={false}
      keyboard={false}
      onOk={() => onAnswer(true)}
      onCancel={() => onAnswer(false)}
    >
      <p>
        Agent 请求调用 <Tag color="red">{info?.tool}</Tag>
        （该工具标记为具有破坏性），请确认参数后选择。
      </p>
      <pre className="confirm-args">{info ? JSON.stringify(info.args, null, 2) : ""}</pre>
    </Modal>
  );
}
