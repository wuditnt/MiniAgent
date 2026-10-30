/**
 * 服务端时钟对齐：执行计时以服务端时间为准，避免本地时钟偏差导致计时跳变。
 * 后端在 run_start / snapshot / history 里带 server_now（秒），这里换成偏移量。
 */
let offsetMs = 0;

export function syncServerNow(serverNow?: number | null): void {
  if (typeof serverNow === "number" && serverNow > 0) {
    offsetMs = serverNow * 1000 - Date.now();
  }
}

/** 当前的服务端时间（毫秒） */
export function serverNowMs(): number {
  return Date.now() + offsetMs;
}
