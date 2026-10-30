/**
 * 运行头行：折叠时一行显示「状态 + 计时 + 最后动作」，展开后是本轮动作时间线。
 *
 * 计时与动作全部来自后端（run_started_at / actions 的 ts），这里只做渲染，
 * 关页面、断线重连后起点不变；本组件每秒重绘一次，用于「执行中」的走表。
 */
import { useEffect, useState } from "react";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  DownOutlined,
  LoadingOutlined,
  UpOutlined,
} from "@ant-design/icons";
import { serverNowMs } from "../serverClock";
import type { RunAction, RunInfo, ToolCall } from "../types";
import ToolChain from "./ToolChain";

/** 毫秒 → m:ss */
function fmtClock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function stateLabel(run: RunInfo): string {
  if (run.status === "running") return "执行中";
  if (run.status === "done") return "完成";
  if (run.status === "cancelled") return "已停止";
  return "失败";
}

/** 最后动作文案：运行中显示「正在 X：参数」，结束后固定为「最后动作 X」 */
function lastActionText(run: RunInfo, streaming: boolean): string {
  const last = run.actions[run.actions.length - 1];
  const running = run.status === "running";
  if (running) {
    if (last && last.status === "running") {
      return last.detail ? `正在 ${last.name}：${last.detail}` : `正在 ${last.name}`;
    }
    return streaming ? "正在生成回答" : "准备中";
  }
  if (!last) return "—";
  return `最后动作 ${last.name}${last.detail ? `：${last.detail}` : ""}`;
}

function actionDot(a: RunAction): string {
  if (a.status === "running") return "live";
  if (a.status === "fail") return "fail";
  if (a.status === "info") return "info";
  return "done";
}

export default function RunHeader({
  run,
  tools,
  streaming,
}: {
  run: RunInfo;
  tools?: ToolCall[];
  streaming: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [, forceTick] = useState(0);

  // 只在执行中走表：结束后用服务端定稿的 elapsed_ms，不再重绘
  useEffect(() => {
    if (run.status !== "running") return;
    const t = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [run.status, run.started_at]);

  const live = run.status === "running";
  const elapsed =
    run.elapsed_ms ?? Math.max(0, Math.round(serverNowMs() - run.started_at * 1000));

  return (
    <div className={`runbox${open ? " open" : ""}`}>
      <button
        type="button"
        className="run-row"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={`run-icon ${live ? "live" : run.status}`}>
          {live ? (
            <LoadingOutlined />
          ) : run.status === "done" ? (
            <CheckCircleFilled />
          ) : (
            <CloseCircleFilled />
          )}
        </span>
        <span className="run-state">{stateLabel(run)}</span>
        <span className="run-time">{fmtClock(elapsed)}</span>
        <span className="run-sep">·</span>
        <span className="run-last">{lastActionText(run, streaming)}</span>
        <span className="run-chev">{open ? <UpOutlined /> : <DownOutlined />}</span>
      </button>
      {open ? (
        <div className="run-timeline">
          {run.actions.map((a, i) => (
            <div className="run-item" key={`${a.ts}-${i}`}>
              <span className="run-item-time">{fmtClock(a.ts)}</span>
              <span className={`run-item-dot ${actionDot(a)}`} />
              <span className="run-item-body">
                <span className="run-item-name">{a.name}</span>
                {a.detail ? <span className="run-item-detail">{a.detail}</span> : null}
              </span>
              {a.dur_ms != null ? (
                <span className="run-item-dur">{(a.dur_ms / 1000).toFixed(1)}s</span>
              ) : null}
            </div>
          ))}
          {tools && tools.length > 0 ? (
            <div className="run-detail-tools">
              <ToolChain tools={tools} />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
