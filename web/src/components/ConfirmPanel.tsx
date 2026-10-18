/**
 * 权限审批面板：confirm_request 事件触发。不是模态框 —— 它覆盖在输入框原来的位置上，
 * 塌缩态恰好盖住整个输入框，点「查看详细」时向上撑开、露出具体参数/脚本。
 *
 * 两种形态：
 *  · 危险工具：一句话人话描述（工具名不进正文），黑色「允许执行」；
 *  · request_approval（auto 档模型主动申请）：描述直接用模型写的申请理由。
 * 只有两个出口（允许/拒绝）；断线或会话结束由上层清空，防止误放行。
 */
import { useEffect, useState } from "react";
import { Button } from "antd";
import { DownOutlined, RightOutlined } from "@ant-design/icons";
import type { ConfirmInfo } from "../types";
import CodeBlock, { guessLang } from "./CodeBlock";

/** 危险工具 → 用户视角的动作短语（不暴露工具名和内部术语） */
const ACTION_TEXT: Record<string, string> = {
  run_shell: "执行一条 shell 命令",
  run_python: "执行一段 Python 代码",
  delete_file: "删除文件或目录",
  move_file: "移动文件或目录",
  edit_file: "修改文件内容",
  re_replace: "批量替换文件内容",
  zip_create: "打包一个压缩包",
  zip_extract: "解压一个压缩包",
  sqlite_query: "执行 SQL 语句",
  image_convert: "转换图片格式",
  git_branch: "切换或查看 git 分支",
  git_commit: "提交 git 改动",
  clipboard_write: "写入系统剪贴板",
  kill_process: "终止进程",
};

/** 描述里顺带点名的关键参数（会有哪个用哪个，拼成「动作：目标」） */
const TARGET_KEYS: Record<string, string[]> = {
  run_shell: ["command"],
  delete_file: ["path"],
  move_file: ["src", "dst"],
  edit_file: ["path"],
  re_replace: ["path"],
  zip_create: ["archive"],
  zip_extract: ["archive"],
  sqlite_query: ["db"],
  image_convert: ["path"],
  git_branch: ["branch"],
  git_commit: ["message"],
  kill_process: ["pid"],
};

/** 值转成一句话里能放下的短文本（太长就截断，完整内容留给「查看详细」） */
function shortValue(v: unknown): string {
  if (typeof v === "string") return v.length > 60 ? `${v.slice(0, 60)}…` : v;
  if (Array.isArray(v)) return v.map(String).join("、");
  if (v && typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** 生成一句用户看得懂的描述 */
function humanDesc(info: ConfirmInfo): string {
  if (info.tool === "request_approval") {
    return String(info.args?.reason ?? "").trim() || "模型申请执行一项有风险的操作。";
  }
  const action = ACTION_TEXT[info.tool] ?? `调用工具「${info.tool}」`;
  const parts: string[] = [];
  for (const key of TARGET_KEYS[info.tool] ?? []) {
    const raw = info.args?.[key];
    if (raw === undefined || raw === null || raw === "") continue;
    parts.push(shortValue(raw));
  }
  const target = parts.length ? `：${parts.join(" → ")}` : "";
  return `Agent 请求${action}${target}，需要你的授权才能继续。`;
}

interface Props {
  info: ConfirmInfo | null;
  onAnswer: (approve: boolean) => void;
  /** 审批等待上限（秒）：来自 /api/state 的 confirm_timeout，超时自动按拒绝处理 */
  timeout?: number;
}

export default function ConfirmPanel({ info, onAnswer, timeout = 120 }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [remaining, setRemaining] = useState(timeout);

  // 换一条确认请求：收起详细区、重置倒计时并开始读秒；归零即按拒绝处理
  useEffect(() => {
    if (!info) return;
    setExpanded(false);
    const total = timeout > 0 ? timeout : 120;
    setRemaining(total);
    const startedAt = Date.now();
    const timer = setInterval(() => {
      const left = Math.max(0, total - Math.floor((Date.now() - startedAt) / 1000));
      setRemaining(left);
      if (left <= 0) {
        clearInterval(timer);
        onAnswer(false);
      }
    }, 250);
    return () => clearInterval(timer);
    // 只在换请求时重建定时器；onAnswer 语义稳定，不进依赖避免反复重置
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info?.id]);

  if (!info) return null;

  const isApproval = info.tool === "request_approval";
  const hasDetail = !isApproval && Object.keys(info.args ?? {}).length > 0;

  return (
    <div className={`confirm-panel${expanded ? " expanded" : ""}`}>
      <div className="confirm-header">
        <div className="confirm-badge">{isApproval ? "等待授权" : "等待审批"}</div>
        <div className="confirm-countdown">你需要在 {remaining} 秒进行审批，否则默认拒绝。</div>
      </div>
      <div className="confirm-desc">{humanDesc(info)}</div>

      {expanded && (
        <div className="confirm-detail">
          <div className="confirm-args-list">
            {Object.entries(info.args ?? {}).map(([key, value]) => (
              <ArgItem key={key} tool={info.tool} name={key} value={value} />
            ))}
          </div>
        </div>
      )}

      <div className="confirm-actions">
        <div className="confirm-actions-left">
          {hasDetail && (
            <Button
              type="text"
              size="small"
              className="confirm-detail-btn"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "收起详细" : "查看详细"}
              {expanded ? <DownOutlined /> : <RightOutlined />}
            </Button>
          )}
        </div>
        <div className="confirm-actions-right">
          <Button onClick={() => onAnswer(false)}>拒绝</Button>
          <Button type="primary" className="confirm-ok-btn" onClick={() => onAnswer(true)}>
            允许执行
          </Button>
        </div>
      </div>
    </div>
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
    const lang =
      typeof value === "string"
        ? guessLang(tool, name, str)
        : (guessLang(tool, name, "") ?? "json");
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
