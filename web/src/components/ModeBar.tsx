/**
 * 头部模式条：联网搜索开关（Switch）。
 * 对话/智能体切换已迁移到输入框下方的工具栏（见 Chat.tsx 的 Sender footer）。
 * 搜索后端来自「系统配置 → 联网搜索」的选择，通过 /api/state 回报给 Tooltip。
 * 快速对话同样可以联网（只是不做推理思考），所以开关不再按模式置灰。
 */
import { GlobalOutlined } from "@ant-design/icons";
import { Switch, Tooltip } from "antd";
import type { AgentState } from "../types";

interface Props {
  web: boolean;
  state: AgentState | null;
  onWeb: (w: boolean) => void;
}

/** 后端 id → 展示名 */
const BACKEND_LABELS: Record<string, string> = {
  deepseek: "DeepSeek 官方搜索",
  tavily: "Tavily",
  bocha: "博查",
  bing: "Bing 网页抓取",
};

export default function ModeBar({ web, state, onWeb }: Props) {
  const backend = state?.search_backend || "unknown";
  const backendText =
    backend === "unknown" || backend === "none"
      ? "尚未确定搜索后端，可在「系统配置 → 联网搜索」里选择"
      : `当前搜索后端：${BACKEND_LABELS[backend] || backend}（可在「系统配置 → 联网搜索」切换）`;

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
