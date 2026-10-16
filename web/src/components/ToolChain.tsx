/**
 * 工具执行轨迹：把 ToolCall[] 映射成 ThoughtChain 节点。
 * tool_start → pending（转圈）；tool_end → success/error + 耗时；参数与结果可展开。
 */
import { CheckCircleFilled, CloseCircleFilled, LoadingOutlined } from "@ant-design/icons";
import { ThoughtChain } from "@ant-design/x";
import { Collapse } from "antd";
import type { ToolCall } from "../types";

function ToolDetail({ call }: { call: ToolCall }) {
  const items = [
    {
      key: "args",
      label: "参数",
      children: <pre>{JSON.stringify(call.args ?? {}, null, 2)}</pre>,
    },
  ];
  if (call.result !== undefined) {
    items.push({
      key: "result",
      label: "结果",
      children: <pre>{call.result || "（空）"}</pre>,
    });
  }
  return <Collapse ghost size="small" className="tool-detail" items={items} />;
}

export default function ToolChain({ tools }: { tools: ToolCall[] }) {
  const items = tools.map((t) => ({
    key: t.id,
    title: t.name,
    status: (t.ok === undefined ? "pending" : t.ok ? "success" : "error") as
      | "pending"
      | "success"
      | "error",
    icon:
      t.ok === undefined ? (
        <LoadingOutlined spin />
      ) : t.ok ? (
        <CheckCircleFilled style={{ color: "#52c41a" }} />
      ) : (
        <CloseCircleFilled style={{ color: "#ff4d4f" }} />
      ),
    description: t.sec !== undefined ? `${t.sec}s` : "执行中…",
    content: <ToolDetail call={t} />,
  }));
  return <ThoughtChain className="tool-chain" items={items} />;
}
