/**
 * 运行轨迹（P0 事实层）：把 /api/trajectory 的事件流渲染成可追溯的时间线。
 *
 * 结构：按 run_id → turn 两级分组；每行 seq / 时间 / type 徽标 / 一行摘要。
 * 交互：行默认折叠，hover 出现「详情」按钮 → CodeBlock 原样展示 payload（不转义换行）；
 *       顶部过滤器（全部 / 模型 / 工具 / 上下文 / 授权 + 只看本轮）；
 *       每个 turn 分组头「回放」→ 前端定时器逐条揭示该轮事件（纯展示，不重发请求）。
 *
 * 数据来源：打开时 fetchTrajectory 拉全量；运行中把父级透传的实时 trace 帧按 seq 并入；
 * 收到 done/error/cancelled 后父级会改变 reloadNonce，触发一次全量兜底拉取。
 */
import { useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  CloseOutlined,
  CodeOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
import { Empty, Segmented, Tooltip } from "antd";
import { fetchTrajectory } from "../api";
import type { TraceEvent, TrajectoryData } from "../types";
import CodeBlock from "./CodeBlock";

interface Props {
  open: boolean;
  /** 要查看轨迹的会话 id（可能与当前聊天会话不同：侧栏「回放」入口） */
  session: string;
  /** 运行中实时镜像的事件（仅当它属于 session 时由父级透传；无则为空数组） */
  liveEvents: TraceEvent[];
  /** 每次「运行结束」父级自增；变化即触发一次全量拉取做磁盘兜底 */
  reloadNonce: number;
  onClose: () => void;
}

type FilterKey = "all" | "model" | "tool" | "context" | "approval";

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "model", label: "模型" },
  { key: "tool", label: "工具" },
  { key: "context", label: "上下文" },
  { key: "approval", label: "授权" },
];

/** type → 过滤器归类（lifecycle 类事件只在「全部」下出现） */
const TYPE_BUCKET: Record<string, FilterKey> = {
  llm_request: "model",
  llm_response: "model",
  tool_call: "tool",
  tool_result: "tool",
  system_prompt: "context",
  user_message: "context",
  context_inject: "context",
  approval: "approval",
};

const WIDTH_KEY = "miniagent.trajectoryWidth";
const MIN_WIDTH = 320;
const MAX_WIDTH = 900;
const DEFAULT_WIDTH = 460;

function loadWidth(): number {
  const raw = Number(localStorage.getItem(WIDTH_KEY));
  if (!Number.isFinite(raw) || raw < MIN_WIDTH) return DEFAULT_WIDTH;
  return Math.min(raw, MAX_WIDTH);
}

/** 时间戳（epoch 秒）→ HH:MM:SS */
function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 一行摘要：把 payload 压成人类可读的短句 */
function summarize(ev: TraceEvent): string {
  const p = ev.payload || {};
  const num = (k: string) => Number(p[k] ?? 0);
  switch (ev.type) {
    case "run_start":
      return `model=${p.model ?? "?"} · 模式=${p.approval ?? "?"}`;
    case "run_end": {
      const u = (p.usage_delta as Record<string, number>) || {};
      const status = p.status === "cancelled" ? "已取消" : p.status === "error" ? "出错" : "完成";
      return `${status} · ${num("sec")}s · ↑${u.prompt_tokens ?? 0} ↓${u.completion_tokens ?? 0}`;
    }
    case "system_prompt":
      return `${num("chars")} 字符 · sha ${String(p.sha256 ?? "").slice(0, 8)}`;
    case "user_message":
      return `${num("chars")} 字符${p.attachments && (p.attachments as unknown[]).length ? ` · ${(p.attachments as unknown[]).length} 个附件` : ""}`;
    case "context_inject": {
      const kind = String(p.kind ?? "");
      const extra =
        kind === "trim_tool_results" ? `${num("count")} 条`
        : kind === "summary" ? `${num("replaced_messages")} 条 → ${num("content") ? String(p.content).length : 0} 字符`
        : kind === "hard_window" ? `丢弃 ${num("dropped_messages")} 条`
        : num("chars") ? `${num("chars")} 字符` : "";
      return `${kind}${extra ? ` · ${extra}` : ""}`;
    }
    case "llm_request":
      return `${p.model ?? "?"} · ${num("message_count")} 条 · ${num("chars")} 字符（仅存摘要）`;
    case "llm_response": {
      const u = (p.usage as Record<string, number>) || {};
      return `${num("sec")}s · ↑${u.prompt_tokens ?? 0} ↓${u.completion_tokens ?? 0}`;
    }
    case "tool_call": {
      const args = JSON.stringify(p.args ?? {});
      return `${p.name ?? "?"}(${args.length > 60 ? `${args.slice(0, 60)}…` : args})`;
    }
    case "tool_result":
      return `${p.name ?? "?"} · ${p.ok ? "成功" : "失败"} · ${p.sec ?? "-"}s · ${num("chars")} 字符`;
    case "approval":
      return `${p.tool ?? "?"} · ${p.decision === "allow" ? "允许" : "拒绝"} · ${p.mode ?? ""}`;
    case "cancel":
      return String(p.reason ?? "已取消");
    case "error":
      return `${p.kind ?? "Error"}: ${String(p.message ?? "").slice(0, 80)}`;
    default:
      return Object.keys(p).length ? JSON.stringify(p).slice(0, 80) : "";
  }
}

/** 展开详情：有正文（content）就原样展示，否则格式化整个 payload */
function detailText(ev: TraceEvent): { text: string; lang?: string } {
  const c = ev.payload?.content;
  if (typeof c === "string" && c) return { text: c };
  return { text: JSON.stringify(ev.payload ?? {}, null, 2), lang: "json" };
}

/** run_id → 事件列表（保持 seq 顺序） */
interface RunGroup {
  runId: string;
  events: TraceEvent[];
}
/** turn 分组键：run_id#turn */
function turnKey(runId: string, turn: number | null): string {
  return `${runId}#${turn ?? "-"}`;
}

export default function Trajectory({
  open,
  session,
  liveEvents,
  reloadNonce,
  onClose,
}: Props) {
  const [fetched, setFetched] = useState<TrajectoryData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<FilterKey>("all");
  const [onlyLatest, setOnlyLatest] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  // 单轮回放：正在回放的 turn 键 + 已揭示行数（null = 未回放，全部可见）
  const [replay, setReplay] = useState<{ key: string; shown: number } | null>(null);
  const [width, setWidth] = useState(loadWidth);
  const [resizing, setResizing] = useState(false);

  // 打开 / 切换会话 / 运行结束兜底 → 全量拉取
  useEffect(() => {
    if (!open || !session) return;
    let alive = true;
    setLoading(true);
    fetchTrajectory(session, 0, 2000)
      .then((d) => {
        if (!alive) return;
        setFetched(d);
        setError("");
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : "读取轨迹失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, session, reloadNonce]);

  // 切换会话/关闭抽屉时重置视图状态
  useEffect(() => {
    setExpanded(new Set());
    setReplay(null);
  }, [session, open]);

  // 合并「磁盘拉取」与「实时镜像」：按 seq 去重，保持 seq 升序
  const merged = useMemo(() => {
    const bySeq = new Map<number, TraceEvent>();
    for (const ev of fetched?.events || []) bySeq.set(ev.seq, ev);
    for (const ev of liveEvents) if (!bySeq.has(ev.seq)) bySeq.set(ev.seq, ev);
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  }, [fetched, liveEvents]);

  // 过滤后的分组：run → turn
  const runs = useMemo<RunGroup[]>(() => {
    const latestReady = merged.length ? merged[merged.length - 1].run_id : "";
    const kept = merged.filter((ev) => {
      if (onlyLatest && ev.run_id !== latestReady) return false;
      if (filter === "all") return true;
      return TYPE_BUCKET[ev.type] === filter;
    });
    const out: RunGroup[] = [];
    for (const ev of kept) {
      const last = out[out.length - 1];
      if (last && last.runId === ev.run_id) last.events.push(ev);
      else out.push({ runId: ev.run_id, events: [ev] });
    }
    return out;
  }, [merged, filter, onlyLatest]);

  // 每个 turn 的行数（回放用）
  const turnTotals = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of runs) {
      for (const ev of r.events) {
        const k = turnKey(r.runId, ev.turn);
        m.set(k, (m.get(k) || 0) + 1);
      }
    }
    return m;
  }, [runs]);

  // 回放定时器：每 320ms 揭示一行，到顶自动停
  useEffect(() => {
    if (!replay) return;
    const total = turnTotals.get(replay.key) || 0;
    if (replay.shown >= total) return;
    const t = setTimeout(() => {
      setReplay((r) => (r && r.key === replay.key ? { ...r, shown: r.shown + 1 } : r));
    }, 320);
    return () => clearTimeout(t);
  }, [replay, turnTotals]);

  const toggleRow = (seq: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });

  const startReplay = (k: string) => {
    setExpanded(new Set());
    setReplay({ key: k, shown: 1 });
  };

  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    setResizing(true);
    const onMove = (ev: MouseEvent) => {
      const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth - (ev.clientX - startX)));
      setWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      setWidth((w) => {
        localStorage.setItem(WIDTH_KEY, String(Math.round(w)));
        return w;
      });
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const className = [
    "workbench",
    "trajectory",
    open ? "open" : "",
    resizing ? "resizing" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <aside
      className={className}
      style={{ ["--wb-width" as string]: `${width}px` }}
      aria-hidden={!open}
    >
      {open && (
        <div
          className="wb-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整轨迹面板宽度"
          onMouseDown={startResize}
          title="拖拽调整宽度"
        />
      )}
      <div className="workbench-inner">
        <div className="wb-tabs">
          <span className="wb-home-label">运行轨迹</span>
          <span className="tj-tabs-hint">
            {merged.length ? `共 ${merged.length} 条事件` : ""}
          </span>
          <Tooltip title="收起轨迹">
            <span className="wb-tab-add" onClick={onClose}>
              <CloseOutlined />
            </span>
          </Tooltip>
        </div>

        <div className="wb-content">
          <div className="tj-toolbar">
            <Segmented
              size="small"
              value={filter}
              onChange={(v) => setFilter(v as FilterKey)}
              options={FILTERS.map((f) => ({ label: f.label, value: f.key }))}
            />
            <span
              className={`tj-only ${onlyLatest ? "on" : ""}`}
              onClick={() => setOnlyLatest((v) => !v)}
            >
              只看本轮
            </span>
          </div>

          {error ? (
            <div className="wb-error">{error}</div>
          ) : merged.length === 0 ? (
            <div className="tj-empty">
              {loading ? (
                <Empty description="读取中…" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : (
                <>
                  <Empty description="无事件记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  <div className="tj-empty-hint">
                    该会话没有事件流（早于 P0 的旧会话，或事件流开关未开启）。
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="tj-timeline">
              {runs.map((run) => {
                const first = run.events[0];
                const last = run.events[run.events.length - 1];
                const dur = Math.max(0, last.ts - first.ts);
                // turn 子分组
                const turns: { turn: number | null; events: TraceEvent[] }[] = [];
                for (const ev of run.events) {
                  const cur = turns[turns.length - 1];
                  if (cur && cur.turn === ev.turn) cur.events.push(ev);
                  else turns.push({ turn: ev.turn, events: [ev] });
                }
                return (
                  <div className="tj-run" key={run.runId}>
                    <div className="tj-run-head">
                      <span className="tj-run-id">{run.runId}</span>
                      <span className="tj-run-meta">
                        {fmtTime(first.ts)} · {dur.toFixed(1)}s · {run.events.length} 条
                      </span>
                    </div>
                    {turns.map((g) => {
                      const k = turnKey(run.runId, g.turn);
                      const playing = replay?.key === k;
                      const rows = playing ? g.events.slice(0, replay!.shown) : g.events;
                      return (
                        <div className="tj-turn" key={k}>
                          <div className="tj-turn-head">
                            <span className="tj-turn-label">
                              {g.turn == null ? "运行" : `第 ${g.turn} 轮`}
                              <span className="tj-turn-count">{g.events.length} 条</span>
                            </span>
                            <Tooltip title="按时间顺序逐条回放这一轮">
                              <span className="tj-replay" onClick={() => startReplay(k)}>
                                <PlayCircleOutlined /> 回放
                              </span>
                            </Tooltip>
                          </div>
                          {rows.map((ev, idx) => {
                            const isOpen = expanded.has(ev.seq);
                            const d = detailText(ev);
                            return (
                              <div
                                className={`tj-row tj-src-${ev.source} ${playing && idx === rows.length - 1 ? "latest" : ""}`}
                                key={ev.seq}
                              >
                                <div className="tj-row-main" onClick={() => toggleRow(ev.seq)}>
                                  <span className="tj-seq">{ev.seq}</span>
                                  <span className="tj-time">{fmtTime(ev.ts)}</span>
                                  <span className={`tj-badge tj-badge-${ev.source}`}>{ev.type}</span>
                                  <span className="tj-summary" title={summarize(ev)}>
                                    {summarize(ev)}
                                  </span>
                                  <Tooltip title={isOpen ? "收起详情" : "查看详情"}>
                                    <span
                                      className={`tj-expand ${isOpen ? "on" : ""}`}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        toggleRow(ev.seq);
                                      }}
                                    >
                                      <CodeOutlined />
                                    </span>
                                  </Tooltip>
                                </div>
                                {isOpen && (
                                  <div className="tj-detail">
                                    <CodeBlock code={d.text} lang={d.lang} maxHeight={320} />
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
