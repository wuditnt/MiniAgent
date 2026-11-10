/**
 * 全局事件流管理器：标签页常驻一条 /api/events 连接，接收「会话创建 / 运行开始 /
 * 运行结束」这类控制信号，驱动列表刷新与历史重拉。
 *
 * 与会话流（sessionStream）不同：
 *  · 这条流不因「空闲」而关 —— 服务端会一直保持到客户端断开，因为随时可能来
 *     session_created（新建会话/自动化后台建会话）。
 *  · 事件不含正文、无 seq，不做回放/去重；断线重连后由 App 主动刷新
 *    running / sessions 兜底，不依赖这条流恢复一致状态。
 */
import { eventsStream } from "./api";
import type { GlobalEvent } from "./types";

let handler: ((ev: GlobalEvent) => void) | null = null;
let ctrl: AbortController | null = null;
let timer: number | null = null;
let retry = 0;
let closed = false;

/** 注册事件处理器（App 每次渲染都调用，保持最新闭包） */
export function setEventStreamHandler(fn: (ev: GlobalEvent) => void): void {
  handler = fn;
}

/** 启动全局事件流（幂等：已有连接时直接返回） */
export function startEventStream(): void {
  // 必须复位：StrictMode 会「挂载→清理→再挂载」把 closed 置真，
  // 登录/登出导致 App 重挂载同理；不复位就再也连不上了
  closed = false;
  if (ctrl) return;
  restart();
}

/** 页面卸载 / 退出登录：彻底关闭 */
export function closeEventStream(): void {
  closed = true;
  stop();
}

function stop(): void {
  ctrl?.abort();
  ctrl = null;
  if (timer != null) {
    window.clearTimeout(timer);
    timer = null;
  }
}

function restart(): void {
  if (closed) return;
  if (timer != null) {
    window.clearTimeout(timer);
    timer = null;
  }
  ctrl?.abort();
  ctrl = null;
  void connect();
}

async function connect(): Promise<void> {
  if (closed) return;
  const ac = new AbortController();
  ctrl = ac;
  try {
    await eventsStream((ev) => {
      retry = 0;
      handler?.(ev);
    }, ac.signal);
  } catch {
    // 网络中断 / 服务端重启：统一走退避重连
  } finally {
    if (ctrl === ac) ctrl = null;
  }
  if (closed || ac.signal.aborted) return;
  // 服务端从不主动关这条流；返回即意味着断开，应重连（含服务端重启）
  retry += 1;
  const delay = Math.min(5000, 400 * 2 ** Math.min(retry, 4));
  timer = window.setTimeout(() => {
    timer = null;
    void connect();
  }, delay);
}

/** 回到前台 / 网络恢复：若没有连接立即补一次 */
function kick(): void {
  if (closed || ctrl) return;
  restart();
}

if (typeof window !== "undefined") {
  window.addEventListener("online", kick);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") kick();
  });
}
