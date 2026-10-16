/**
 * 头部模式条：对话/智能体切换（Segmented）+ 联网搜索开关（Switch）。
 * 搜索后端探测结果通过 Tooltip 展示（bing / tavily / bocha / unknown）。
 */
import { GlobalOutlined, MessageOutlined, RobotOutlined } from "@ant-design/icons";
import { Segmented, Switch, Tooltip } from "antd";
import type { AgentState, Mode } from "../types";

interface Props {
  mode: Mode;
  web: boolean;
  state: AgentState | null;
  onMode: (m: Mode) => void;
  onWeb: (w: boolean) => void;
}

export default function ModeBar({ mode, web, state, onMode, onWeb }: Props) {
  const backend = state?.search_backend || "unknown";
  const backendText =
    backend === "unknown" || backend === "none"
      ? "未检测到可用搜索后端（可在 .env 配置 TAVILY_API_KEY / BOCHA_API_KEY，无 key 时用 Bing 兜底）"
      : `当前搜索后端：${backend}`;

  return (
    <div className="mode-bar">
      <Segmented
        value={mode}
        onChange={(v) => onMode(v as Mode)}
        options={[
          { value: "chat", label: "对话", icon: <MessageOutlined /> },
          { value: "agent", label: "智能体", icon: <RobotOutlined /> },
        ]}
      />
      <Tooltip title={backendText}>
        <span className="web-switch">
          <GlobalOutlined />
          <Switch size="small" checked={web} onChange={onWeb} />
          联网搜索
        </span>
      </Tooltip>
    </div>
  );
}
