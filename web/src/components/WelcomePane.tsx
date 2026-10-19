/**
 * 空状态欢迎页：Welcome + Prompts 快捷指令，chat / agent 各一套。
 * 点击卡片直接发送对应的完整 prompt（prompt 字段是本项目自定义扩展）。
 */
import {
  BranchesOutlined,
  CodeOutlined,
  FileExcelOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MessageOutlined,
  RobotOutlined,
  TranslationOutlined,
} from "@ant-design/icons";
import { Prompts, Welcome } from "@ant-design/x";
import type { Mode } from "../types";

const CHAT_PROMPTS = [
  {
    key: "write",
    icon: <FileTextOutlined />,
    label: "帮我写一份周报",
    description: "按要点整理成本周工作汇报",
    prompt: "帮我写一份本周工作周报，主要工作是：完成 mini-agent 的 Web UI 开发。要求语气专业、结构清晰。",
  },
  {
    key: "translate",
    icon: <TranslationOutlined />,
    label: "中译英",
    description: "地道、自然的技术翻译",
    prompt: "把下面这句话翻译成英文，保持技术语境准确：本地部署的智能体让数据不出内网。",
  },
  {
    key: "qa",
    icon: <MessageOutlined />,
    label: "自由问答",
    description: "直接在下方输入问题",
    prompt: "用三句话介绍你能做什么，以及和普通聊天机器人的区别。",
  },
];

const AGENT_PROMPTS = [
  {
    key: "tree",
    icon: <FolderOpenOutlined />,
    label: "浏览工作区",
    description: "列出根目录内容",
    prompt: "列出工作区根目录下的文件和子目录，并简要说明这个项目是做什么的。",
  },
  {
    key: "loc",
    icon: <CodeOutlined />,
    label: "统计代码行数",
    description: "Python 源码规模",
    prompt: "统计工作区下所有 Python 源码文件的数量和总行数。",
  },
  {
    key: "git",
    icon: <BranchesOutlined />,
    label: "git 状态",
    description: "查看仓库当前状态",
    prompt: "运行 git status，告诉我当前仓库有什么未提交的改动。",
  },
  {
    key: "csv",
    icon: <FileExcelOutlined />,
    label: "分析 CSV",
    description: "读取并汇总数据文件",
    prompt: "看看工作区里有没有 CSV 文件，如果有就读取并给出简要统计。",
  },
];

export default function WelcomePane({
  mode,
  onPick,
}: {
  mode: Mode;
  onPick: (text: string) => void;
}) {
  const items = mode === "chat" ? CHAT_PROMPTS : AGENT_PROMPTS;
  return (
    <div className="welcome-pane">
      <Welcome
        variant="borderless"
        icon={<RobotOutlined className="welcome-icon" />}
        title="你好，我是 mini-agent"
        description={
          mode === "chat"
            ? "快速对话：直接回答，不做推理思考（可开启联网搜索）"
            : "智能体模式：我会调用本地工具（文件 / 代码 / 系统 / Git 等）帮你完成任务"
        }
      />
      <Prompts
        wrap
        items={items as any}
        onItemClick={(info: any) => {
          const picked = info?.data;
          if (picked?.prompt) onPick(picked.prompt);
        }}
      />
    </div>
  );
}
