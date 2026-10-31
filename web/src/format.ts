/**
 * 耗时格式：全站唯一实现，保证「运行头部」「消息脚注」「工具动作」写法一致。
 *   6s / 12.3s / 1m23s
 */

/** 秒 → 「Xs」或「XmYs」（<60s 保留一位小数，≥60s 按分+秒） */
export function fmtSec(secRaw: number): string {
  const sec = Number.isFinite(secRaw) ? Math.max(0, secRaw) : 0;
  if (sec < 59.95) {
    const v = Math.round(sec * 10) / 10;
    return `${v}s`;
  }
  const total = Math.round(sec);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m${s}s`;
}
