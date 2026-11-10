/**
 * 会话实时流管理器：一个标签页只常驻一条 SSE，同时观看多个会话。
 *
 * 设计要点（对应后端 /api/chat/stream?sessions=…）：
 *  · 兴趣集只增不减时不重连；只有「新增会话」才重连，并只为新增的会话要整份快照，
 *    其余会话靠 Last-Event-ID 续传 —— 不会因为开了一个后台任务就把别的会话整份重建。
 *  · 帧按 session 分流，按 seq 去重：服务端全局单调序号，重复/回放帧直接丢掉。
 *  · 断线自动重连（指数退避）；服务端判定无法续传时会自动回落成快照。
 *  · 正常关流（订阅的会话都不在运行）不立刻重连，等兴趣集变化或页面回到前台再说，
 *    避免「空闲会话刚结束 → 服务端关流 → 客户端立刻重连」的空转。
 */
import { multiStream } from "./api";
import type { SSEEvent } from "./types";

let handler: ((ev: SSEEvent) => void) | null = null;
let desired: string[] = [];
let prevDesired: string[] = [];
let lastSeq = 0;
let ctrl: AbortController | null = null;
let timer: number | null = null;
let retry = 0;
let closed = false;

/** 注册帧处理器（App 每次渲染都会调用，保持最新闭包） */
export function setStreamHandler(fn: (ev: SSEEvent) => void): void {
  handler = fn;
}

/** 设置本页关心的会话集合；只有新增会话才会触发重连 */
export function setInterest(next: string[]): void {
  const uniq = [...new Set(next.filter(Boolean))].sort();
  // 有订阅需求就复位 closed：StrictMode 的「挂载→清理→再挂载」会把 closed 置真，
  // 登录/登出重挂载同理；不复位则共享流永久失效（只剩轮询出来的运行点）
  if (uniq.length) closed = false;
  const added = uniq.filter((sid) => !prevDesired.includes(sid));
  const same = uniq.length === prevDesired.length &&
    uniq.every((sid, i) => sid === prevDesired[i]);
  prevDesired = uniq;
  desired = uniq;
  if (same) return;
  if (!uniq.length) {
    stop();
    return;
  }
  // 只是减少订阅：现有连接继续用（多余会话的帧在 onFrame 里被忽略），不打断别的流
  if (!added.length && ctrl) return;
  restart(added);
}

/** 页面卸载 / 退出登录：彻底关闭 */
export function closeSessionStream(): void {
  closed = true;
  stop();
  desired = [];
  prevDesired = [];
  lastSeq = 0;
}

function stop(): void {
  ctrl?.abort();
  ctrl = null;
  if (timer != null) {
    window.clearTimeout(timer);
    timer = null;
  }
}

function restart(needSnap: string[]): void {
  if (closed) return;
  if (timer != null) {
    window.clearTimeout(timer);
    timer = null;
  }
  ctrl?.abort();
  ctrl = null;
  void connect(needSnap);
}

async function connect(needSnap: string[]): Promise<void> {
  if (closed || !desired.length) return;
  const ac = new AbortController();
  ctrl = ac;
  let clean = false;
  try {
    await multiStream(
      desired,
      lastSeq,
      needSnap,
      (ev) => {
        retry = 0;
        if (ev.seq != null) {
          if (ev.seq <= lastSeq) return;   // 回放/重复帧
          lastSeq = ev.seq;
        }
        handler?.(ev);
      },
      ac.signal
    );
    clean = true;
  } catch {
    // 网络中断 / 服务端异常：走退避重连（带 lastSeq 续传）
  } finally {
    if (ctrl === ac) ctrl = null;
  }
  if (closed || ac.signal.aborted) return;
  if (clean) return;   // 服务端主动关流：等兴趣集变化或回到前台再连
  retry += 1;
  const delay = Math.min(5000, 400 * 2 ** Math.min(retry, 4));
  timer = window.setTimeout(() => {
    timer = null;
    void connect([]);
  }, delay);
}

/** 回到前台 / 网络恢复：若当前没有连接，立即补一次（不等退避） */
function kick(): void {
  if (closed || !desired.length || ctrl) return;
  restart([]);
}

if (typeof window !== "undefined") {
  window.addEventListener("online", kick);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") kick();
  });
}
