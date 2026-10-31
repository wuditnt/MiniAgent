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

/** 消息时间：今天 23:08 / 昨天 23:08 / 10-31 23:08 / 2025-10-31 23:08 */
export function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${hm}`;
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  if (d.getFullYear() === now.getFullYear()) return `${md} ${hm}`;
  return `${d.getFullYear()}-${md} ${hm}`;
}
