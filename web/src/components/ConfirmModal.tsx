/**
 * 确认弹窗：confirm_request 事件触发。有两种形态：
 *  · 普通危险工具：展示工具名 + 参数，红色危险按钮；
 *  · request_approval（auto 档模型主动申请）：展示申请理由，常规主按钮。
 * 只有两个出口（允许/拒绝），关闭按钮与 Esc 都映射为「拒绝」，防止误放行。
 */
import { Modal, Tag } from "antd";
import { SafetyCertificateOutlined, WarningOutlined } from "@ant-design/icons";
import type { ConfirmInfo } from "../types";
import CodeBlock, { guessLang } from "./CodeBlock";

interface Props {
  info: ConfirmInfo | null;
  onAnswer: (approve: boolean) => void;
}

export default function ConfirmModal({ info, onAnswer }: Props) {
  const isApproval = info?.tool === "request_approval";
  const reason = isApproval ? String(info?.args?.reason ?? "") : "";

  if (isApproval) {
    return (
      <Modal
        open={!!info}
        title={
          <span>
            <SafetyCertificateOutlined style={{ color: "#1677ff", marginRight: 8 }} />
            模型请求授权
          </span>
        }
        okText="批准"
        cancelText="拒绝"
        closable={false}
        maskClosable={false}
        keyboard={false}
        onOk={() => onAnswer(true)}
        onCancel={() => onAnswer(false)}
      >
        <p style={{ color: "#595959", marginBottom: 8 }}>
          Agent 在自主判断模式下认为接下来的操作有风险，执行前需要你确认：
        </p>
        <div className="confirm-reason">{reason}</div>
      </Modal>
    );
  }

  return (
    <Modal
      open={!!info}
      title={
        <span>
          <WarningOutlined style={{ color: "#ff4d4f", marginRight: 8 }} />
          危险操作确认
        </span>
      }
      okText="允许执行"
      cancelText="拒绝"
      okButtonProps={{ danger: true }}
      closable={false}
      maskClosable={false}
      keyboard={false}
      width={720}
      onOk={() => onAnswer(true)}
      onCancel={() => onAnswer(false)}
    >
      <p>
        Agent 请求调用 <Tag color="red">{info?.tool}</Tag>
        （该工具标记为具有破坏性），请确认参数后选择。
      </p>
      <div className="confirm-args-list">
        {Object.entries(info?.args ?? {}).map(([key, value]) => (
          <ArgItem key={key} tool={info?.tool} name={key} value={value} />
        ))}
      </div>
    </Modal>
  );
}

/** 单个参数：长文本/多行/代码类内容整块高亮展示（原始未转义），短值行内键值对。 */
function ArgItem({ tool, name, value }: { tool?: string; name: string; value: unknown }) {
  // 先统一成字符串：str → 原值；dict/list → 格式化 JSON；其他 → String()
  const str =
    typeof value === "string"
      ? value
      : value === null || value === undefined
        ? String(value)
        : Array.isArray(value) || typeof value === "object"
          ? JSON.stringify(value, null, 2)
          : String(value);

  if (str.length > 40 || str.includes("\n")) {
    const lang = typeof value === "string"
      ? guessLang(tool, name, str)
      : guessLang(tool, name, "") ?? "json";
    return (
      <div className="confirm-arg">
        <div className="confirm-arg-name">{name}</div>
        <div className="confirm-arg-code">
          <CodeBlock code={str} lang={lang} maxHeight={320} />
        </div>
      </div>
    );
  }
  return (
    <div className="confirm-arg confirm-arg-inline">
      <span className="confirm-arg-name">{name}</span>
      <span className="confirm-arg-value">{str}</span>
    </div>
  );
}
