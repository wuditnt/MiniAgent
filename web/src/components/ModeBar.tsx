/**
 * 头部模式条：联网搜索开关（Switch）。
 * 对话/智能体切换已迁移到输入框下方的工具栏（见 Chat.tsx 的 Sender footer）。
 * 搜索后端探测结果通过 Tooltip 展示（bing / tavily / bocha / unknown）。
 */
import { GlobalOutlined } from "@ant-design/icons";
import { Switch, Tooltip } from "antd";
import type { AgentState } from "../types";

interface Props {
  web: boolean;
  state: AgentState | null;
  onWeb: (w: boolean) => void;
}

export default function ModeBar({ web, state, onWeb }: Props) {
  const backend = state?.search_backend || "unknown";
  const backendText =
    backend === "unknown" || backend === "none"
      ? "未检测到可用搜索后端（可在 .env 配置 TAVILY_API_KEY / BOCHA_API_KEY，无 key 时用 Bing 兜底）"
      : `当前搜索后端：${backend}`;

  return (
    <div className="mode-bar">
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
