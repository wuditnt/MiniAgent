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
    desc: "每周一监控品牌全渠道舆情，输出情绪分布、风险预警与运营建议。",
    schedule: { kind: "weekly", weekdays: [1], time: "09:00" },
    web: true,
    prompt:
      "请监控以下品牌/产品在过去一周内的网络舆情：\n" +
      "- [品牌名称]\n" +
      "监控渠道：\n" +
      "1. 社交媒体（Twitter/X、Reddit、知乎、微博）\n" +
      "2. 技术社区（Hacker News、V2EX、ProductHunt）\n" +
      "3. 媒体报道和博客文章\n" +
      "分析维度：\n" +
      "1. 本周提及量趋势（相比上周是增是减）\n" +
      "2. 情绪分布：正面、中性、负面评价各占多少，概括各类别的代表性观点\n" +
      "3. 高热度话题：本周被讨论最多的 3-5 个产品体验或商业相关话题及其核心观点\n" +
      "4. 风险预警：是否有产品质量、服务体验或商业决策相关的负面舆情发酵迹象\n" +
      "输出要求：\n" +
      "- 开头用一段话总结本周舆情整体态势\n" +
      "- 按上述维度结构化输出\n" +
      "- 标注信息来源链接\n" +
      "- 最后给出 1-3 条运营行动建议",
  },
  {
    key: "competitor-weekly",
    name: "每周竞品动态追踪",
    desc: "每周一从产品、社区、商业三维度扫描竞品动态与竞争格局。",
    schedule: { kind: "weekly", weekdays: [1], time: "10:00" },
    web: true,
    prompt:
      "我需要对以下竞争对手进行一次周度情报扫描：\n" +
      "- [竞品 A]\n" +
      "- [竞品 B]\n" +
      "- [竞品 C]\n" +
      "请从以下几个维度收集信息：\n" +
      "**产品变化**：近一周内是否有版本迭代、功能上线或 changelog 更新（覆盖官网、GitHub Releases、社交媒体公告）\n" +
      "**社区反馈**：开发者社区（Reddit、Twitter/X、ProductHunt、Hacker News 等）中围绕这些产品的使用体验讨论与评价倾向\n" +
      "**商业动向**：是否有融资、战略合作、团队扩张、重大客户签约等公开报道\n" +
      "输出格式：\n" +
      "- 以竞品为单位分段呈现，每段包含上述三个维度的关键发现\n" +
      "- 每条信息附上原始来源链接\n" +
      "- 末尾输出一段「本周竞争格局速览」，概括产品和商业层面的态势变化与值得关注的信号",
  },
  {
    key: "stock-watch",
    name: "股价监控与预警",
    desc: "每个交易日收盘后检查关注 A 股的价格变动与异常波动。",
    schedule: { kind: "weekly", weekdays: [1, 2, 3, 4, 5], time: "15:30" },
    web: true,
    prompt:
      "请查询以下股票的最新价格和昨日表现：\n" +
      "- 贵州茅台（600519）\n" +
      "- 宁德时代（300750）\n" +
      "- 比亚迪（002594）\n" +
      "输出要求：\n" +
      "1. 每只股票列出：当前价格、昨日涨跌幅、近一周趋势（上涨/下跌/震荡）\n" +
      "2. 如果任一股票单日涨跌幅超过 3%，标记为“⚠️ 异常波动”并从财报、产品发布或行业趋势角度简要分析可能原因\n" +
      "3. 最后附一段今日大盘概况（上证指数 / 深证成指 / 创业板指走势）",
  },
  {
    key: "security-scan",
    name: "安全漏洞扫描",
    desc: "每周五对仓库做安全审计，只报告中高危且可利用的漏洞。",
    schedule: { kind: "weekly", weekdays: [5], time: "18:00" },
    web: false,
    needsRepo: true,
    prompt:
      "你是一个自动化安全审计工具，负责对本代码仓库进行周期性的漏洞评估。\n" +
      "目标：识别中等严重度及以上的已确认漏洞，且必须具备可论证的端到端利用路径。不报告理论性或推测性风险。\n" +
      "审计流程：\n" +
      "首先，梳理代码库架构——理解入口点、信任边界以及组件间的数据流转。\n" +
      "然后，按以下分组系统性地检查高风险攻击面：\n" +
      "- **认证与访问控制**：登录流程、会话管理、角色/权限校验\n" +
      "- **注入向量**：原始 SQL 查询、Shell 命令拼接、模板渲染、文件路径操作\n" +
      "- **外部交互**：Webhook 处理器、出站网络请求、第三方 API 集成\n" +
      "- **敏感数据处理**：代码或配置中的密钥、凭证或 PII 的日志记录、加密实践\n" +
      "对每个潜在发现，从攻击者可控输入到影响结果追踪完整代码路径。仅保留能具体证明可利用性的发现。\n" +
      "证据要求——每个报告的问题必须清晰说明：\n" +
      "- 攻击者画像（外部用户、已认证用户、内部服务等）\n" +
      "- 其可控的输入向量\n" +
      "- 从输入到漏洞的确切代码路径\n" +
      "- 造成的影响（数据泄露、权限提升、拒绝服务等）\n" +
      "- 建议的修复方案\n" +
      "输出：如发现已确认的问题，按严重度分组输出结构化报告，包含位置、影响和修复建议。如未确认中等及以上漏洞，输出：“审计完成——未发现中等或更高严重度的已确认漏洞。”",
  },
  {
    key: "commit-bug-scan",
    name: "提交后缺陷检查与修复",
    desc: "每天下班后检查过去 24 小时的提交，定位并最小化修复高影响缺陷。",
    schedule: { kind: "daily", time: "18:30" },
    web: false,
    needsRepo: true,
    prompt:
      "你是一个提交后正确性检查工具，专注于发现高影响缺陷。\n" +
      "目标：分析过去 24 小时内的提交，找出在代码审查中遗漏的关键 Bug。仅标记会导致数据丢失、应用崩溃、安全漏洞或用户可感知的严重功能退化的问题。\n" +
      "关注重点——优先检查影响范围广的行为变更：\n" +
      "- **数据完整性问题**：数据损坏、静默截断、错误的数据转换\n" +
      "- **并发缺陷**：竞态条件、死锁、不安全的共享状态\n" +
      "- **崩溃风险路径**：关键流程中的空引用/未定义引用、未处理的异常\n" +
      "- **安全缺口**：认证或权限绕过、输入校验遗漏\n" +
      "- **资源管理**：泄漏（内存、文件句柄、连接）、无限循环或无界分配\n" +
      "排查方法：对每个可疑变更追踪完整的执行路径——不要仅依赖 diff 层面的模式匹配。理解上游调用方和下游影响。\n" +
      "报告门槛：必须能描述一个具体的、可信的触发场景。如果无法构造出具体的触发条件，则不进行报告。\n" +
      "发现确认的 Bug 时：\n" +
      "- 实施最小化、高置信度的定向修复\n" +
      "- 添加或更新测试以覆盖缺陷行为\n" +
      "- 不要在修复中混入大范围重构\n" +
      "未发现关键 Bug 时：\n" +
      "- 输出：“过去 24 小时的提交中未发现关键缺陷。”这是大多数运行的预期结果。",
  },
  {
    key: "daily-changelog",
    name: "每日变更摘要",
    desc: "每天汇总过去 24 小时的变更，生成按主题归类的工程变更日志。",
    schedule: { kind: "daily", time: "19:00" },
    web: true,
    needsRepo: true,
    prompt:
      "你是本仓库的自动化变更日志整理工具。\n" +
      "目标：生成过去 24 小时内所有有意义变更的精炼、高价值摘要，便于工程团队快速浏览。\n" +
      "需要涵盖的内容：\n" +
      "**重要合并**：突出引入新能力、改变系统行为或影响用户侧功能的已合并 PR，简要说明每项的意图和范围。\n" +
      "**稳定性与可靠性**：标注 Bug 修复、事故相关补丁，以及涉及易出错或历史上脆弱区域的变更。\n" +
      "**安全与依赖**：标记安全补丁、依赖升级或影响项目风险态势的配置变更。\n" +
      "**待跟进事项**：识别需要后续关注的条目——如缺少测试的已合并代码、有发布风险的变更、潜在回归问题或 diff 中遗留的 TODO。\n" +
      "指导原则：\n" +
      "- 重相关性而非完整性——省略琐碎或常规变更\n" +
      "- 将相关变更归纳为连贯的主题，而非逐条罗列\n" +
      "- 每条陈述都基于提交或 PR 的具体证据，不做推测\n" +
      "- 保持输出易于扫读——使用简洁语言和清晰结构\n" +
      "输出格式：\n" +
      "- 注明覆盖的日期范围\n" +
      "- 按上述主题组织关键变更（总计 4–8 条为宜）\n" +
      "- 包含一个「需要关注」部分，列出 1–3 个有风险或需后续行动的事项\n" +
      "- 如该时段内无有意义的变更，输出：“过去 24 小时内无重大变更记录。”",
  },
];
