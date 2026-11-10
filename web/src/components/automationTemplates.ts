/**
 * 自动化任务的前端常量与纯函数：内置模板、规则文案、排期计算。
 *
 * 与后端 core/automation.py 的 next_after 语义保持一致：
 *   · 返回值严格晚于基准时刻
 *   · daily / weekly 按本机时区
 *   · interval 从基准起算（不补偿停机时间）
 * 只用于「编辑器预览」，真正落库的 next_run_at 由后端计算。
 */
import type { AutomationSchedule, AutomationScheduleKind } from "../types";

export const WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export const MIN_INTERVAL_MINUTES = 5;

/** 执行结果的展示文案（与后端 task_runs.status 对齐） */
export const STATUS_LABELS: Record<string, string> = {
  success: "成功",
  failed: "失败",
  skipped: "已跳过",
  aborted: "已中断",
  running: "执行中",
};

const pad2 = (n: number) => (n < 10 ? `0${n}` : `${n}`);

export function fmtDateTime(ts: number): string {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(
    d.getHours()
  )}:${pad2(d.getMinutes())}`;
}

export function fmtClock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** 相对时间：3 分钟后 / 2 小时后 / 明天 07:00 / 已过期 */
export function fmtRelative(ts: number, nowSec: number = Date.now() / 1000): string {
  if (!ts) return "-";
  const diff = ts - nowSec;
  if (diff <= -60) return "已过期";
  if (diff < 0) return "即将执行";
  if (diff < 60) return "1 分钟内";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟后`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时后`;
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)} 天后`;
  return fmtDateTime(ts);
}

/** 严格晚于 baseSec 的下一个触发时刻；单次已过期返回 null */
export function nextAfter(schedule: AutomationSchedule, baseSec: number): number | null {
  if (schedule.kind === "once") {
    return schedule.at > baseSec ? schedule.at : null;
  }
  if (schedule.kind === "interval") {
    return baseSec + schedule.every_minutes * 60;
  }
  const [hour, minute] = (schedule.time || "00:00").split(":").map(Number);
  if (schedule.kind === "daily") {
    const cand = new Date(baseSec * 1000);
    cand.setHours(hour, minute, 0, 0);
    if (cand.getTime() / 1000 <= baseSec) cand.setDate(cand.getDate() + 1);
    return cand.getTime() / 1000;
  }
  // weekly：1=周一 … 7=周日（与 isoweekday 对齐）
  const days = new Set(schedule.weekdays);
  for (let offset = 0; offset <= 7; offset += 1) {
    const day = new Date(baseSec * 1000);
    day.setDate(day.getDate() + offset);
    const iso = day.getDay() === 0 ? 7 : day.getDay();
    if (!days.has(iso)) continue;
    day.setHours(hour, minute, 0, 0);
    if (day.getTime() / 1000 > baseSec) return day.getTime() / 1000;
  }
  return null;
}

export function nextRuns(
  schedule: AutomationSchedule,
  count = 3,
  baseSec: number = Date.now() / 1000
): number[] {
  const out: number[] = [];
  let cur = baseSec;
  for (let i = 0; i < count; i += 1) {
    const nxt = nextAfter(schedule, cur);
    if (nxt === null) break;
    out.push(nxt);
    cur = nxt;
  }
  return out;
}

export function describeSchedule(schedule: AutomationSchedule): string {
  if (schedule.kind === "once") {
    return schedule.at ? `单次 ${fmtDateTime(schedule.at)}` : "单次（未设置时间）";
  }
  if (schedule.kind === "daily") return `每天 ${schedule.time}`;
  if (schedule.kind === "weekly") {
    const days = [...schedule.weekdays]
      .sort((a, b) => a - b)
      .map((d) => WEEKDAY_NAMES[d - 1] || "")
      .join("、");
    return `每周${days} ${schedule.time}`;
  }
  const every = schedule.every_minutes;
  return every % 60 === 0 ? `每 ${every / 60} 小时` : `每 ${every} 分钟`;
}

/** 切换重复方式时给一个合理的默认排期（避免表单出现空值） */
export function defaultSchedule(kind: AutomationScheduleKind): AutomationSchedule {
  if (kind === "once") {
    const at = new Date(Date.now() + 3600 * 1000);
    at.setSeconds(0, 0);
    return { kind: "once", at: Math.round(at.getTime() / 1000) };
  }
  if (kind === "daily") return { kind: "daily", time: "07:00" };
  if (kind === "weekly") return { kind: "weekly", weekdays: [1], time: "09:00" };
  return { kind: "interval", every_minutes: 60 };
}

/** datetime-local 输入框 ↔ epoch 秒 */
export function toDatetimeInput(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(
    d.getHours()
  )}:${pad2(d.getMinutes())}`;
}

export function fromDatetimeInput(value: string): number {
  const ts = new Date(value).getTime();
  return Number.isFinite(ts) ? Math.round(ts / 1000) : 0;
}

// ---------------------------------------------------------------- 内置模板

export interface AutomationTemplate {
  key: string;
  name: string;
  desc: string;
  prompt: string;
  schedule: AutomationSchedule;
  web: boolean;
  /** 依赖工作区里有代码仓库（创建时只提示，不阻止） */
  needsRepo?: boolean;
}

export const AUTOMATION_TEMPLATES: AutomationTemplate[] = [
  {
    key: "ai-daily",
    name: "每日 AI 新闻简报",
    desc: "每天早上汇总 AI 行业热点，要点 + 来源 + 趋势点评。",
    schedule: { kind: "daily", time: "07:00" },
    web: true,
    prompt:
      "搜索今日 AI 行业的热点新闻，覆盖以下方面：\n" +
      "1. 重要产品发布或功能更新（如 OpenAI、Google、Anthropic 等公司动态）\n" +
      "2. 融资事件与行业并购\n" +
      "3. 技术突破或重要论文发布\n" +
      "4. 行业标准与规范动态（如 AI 安全框架、数据治理标准的进展）\n" +
      "输出要求：\n" +
      "- 按重要性排序，列出 5-8 条新闻\n" +
      "- 每条新闻包含：标题、一句话摘要、信息来源\n" +
      "- 聚焦技术进展和商业动态层面\n" +
      "- 最后附一段 2-3 句的“今日趋势点评”",
  },
  {
    key: "brand-weekly",
    name: "品牌舆情监控周报",
    desc: "每周一汇总品牌在社媒与社区的提及和评价。",
    schedule: { kind: "weekly", weekdays: [1], time: "09:00" },
    web: true,
    prompt:
      "检索过去一周品牌名在主流社交媒体、社区与新闻中的提及，输出：\n" +
      "1. 舆情概览（提及量级与正负面分布）\n2. 典型案例（正面/负面各 3-5 条，附链接与原话摘录）\n" +
      "3. 风险提示与应对建议\n4. 下周重点观察对象\n",
  },
  {
    key: "competitor-weekly",
    name: "每周竞品动态追踪",
    desc: "每周一追踪竞品产品更新、社区反馈与重要新闻。",
    schedule: { kind: "weekly", weekdays: [1], time: "09:00" },
    web: true,
    prompt:
      "追踪指定竞品过去一周的动态：产品功能更新、定价与商业化变化、社区反馈、重要新闻与人事变动。\n" +
      "输出：\n1. 变化清单（竞品 × 变化点 × 影响判断）\n2. 与我方产品的差异对比\n3. 建议跟进的动作\n",
  },
  {
    key: "stock-watch",
    name: "股价监控与预警",
    desc: "每天收盘后检查关注股票的价格变动与异常波动。",
    schedule: { kind: "daily", time: "15:30" },
    web: true,
    prompt:
      "查询我关注的股票当日行情：收盘价、涨跌幅、成交额与近期波动。\n" +
      "若单日涨跌幅超过 5% 或出现明显异常，给出可能的原因线索与相关新闻链接；\n" +
      "输出：1. 行情一览表 2. 异常波动说明 3. 需要关注的风险点。",
  },
  {
    key: "security-scan",
    name: "安全漏洞扫描",
    desc: "每周五扫描工作区仓库，验证中高危漏洞并给出修复建议。",
    schedule: { kind: "weekly", weekdays: [5], time: "18:00" },
    web: false,
    needsRepo: true,
    prompt:
      "扫描当前工作区仓库的代码，重点检查：注入（SQL/命令/模板）、越权与鉴权缺失、\n" +
      "敏感信息硬编码、不安全反序列化、路径穿越、依赖高危版本。\n" +
      "对每个疑似问题先读代码验证，确认可达后再报告；输出：文件:行、证据片段、\n" +
      "风险等级、修复建议。无法确认的只列为「待人工确认」，不要臆测。",
  },
  {
    key: "commit-bug-scan",
    name: "扫描提交发现 Bug",
    desc: "每天分析最近提交，定位可能导致严重后果的高危 Bug。",
    schedule: { kind: "daily", time: "09:00" },
    web: false,
    needsRepo: true,
    prompt:
      "分析当前仓库最近 10 个提交的变更内容，定位可能导致严重后果的高危 Bug\n" +
      "（数据丢失、并发竞态、空指针、边界条件、资源泄漏、错误处理缺失）。\n" +
      "每个问题给出：涉及提交、文件:行、触发条件、影响面、修复建议。\n" +
      "只报告能自证的问题，风格问题不要提。",
  },
  {
    key: "test-coverage",
    name: "补充测试覆盖",
    desc: "每周三找出最近变更中缺少测试的高风险代码并补测试。",
    schedule: { kind: "weekly", weekdays: [3], time: "10:00" },
    web: false,
    needsRepo: true,
    prompt:
      "检查最近两周变更中缺少测试覆盖的高风险代码（核心业务逻辑、边界处理、错误分支），\n" +
      "为其中最重要的 3-5 处补充单元测试，并在本地运行通过。\n" +
      "输出：新增/修改的测试文件、覆盖的场景、运行结果；无法补测的说明原因。",
  },
  {
    key: "daily-changelog",
    name: "每日变更摘要",
    desc: "每天汇总仓库当日变更，生成团队可读的工程日报。",
    schedule: { kind: "daily", time: "18:00" },
    web: false,
    needsRepo: true,
    prompt:
      "汇总当前仓库当日的全部提交与变更，生成工程日报：\n1. 今日完成（按模块归类，写清用户可感知的变化）\n" +
      "2. 修复的问题\n3. 风险与待办（未完成项、需要评审的改动）\n" +
      "面向团队同学，语言简洁，不要逐条罗列 commit hash。",
  },
];
