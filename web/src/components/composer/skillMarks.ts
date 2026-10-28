/**
 * 输入框里「技能文字块」的编码约定。
 *
 * 两套字符，互不冲突：
 *  · 输入框内部：`\u0002skill:<来源>:<名字>\u0002`，用来把块渲染成不可编辑的胶囊；
 *  · 发送给后端：`\u0001<名字>\u0001`，裸名字 + 不可见包裹字符 —— 后端在消息里
 *    查找技能时既不会误判正文，用户也看不见这两个字符。
 */
export type SkillSource = "builtin" | "custom";

export interface ComposerSkill {
  name: string;
  source: SkillSource;
}

const TOKEN_RE = /\u0002skill:(builtin|custom):([^\u0002]*)\u0002/g;
const SENT_RE = /\u0001([^\u0001\n]{1,64})\u0001/g;

/** 输入框内部的技能块标记 */
export function skillToken(name: string, source: SkillSource): string {
  return `\u0002skill:${source}:${name}\u0002`;
}

/** 拆出技能块与用户正文（正文只去掉标记，不 trim —— 交给调用方决定） */
export function parseComposer(value: string): { skills: ComposerSkill[]; text: string } {
  const skills: ComposerSkill[] = [];
  const text = value.replace(TOKEN_RE, (_m, source: string, name: string) => {
    skills.push({ name, source: source as SkillSource });
    return "";
  });
  return { skills, text };
}

/** 发送前序列化：技能块 → `\u0001名字\u0001`，其余原样保留 */
export function serializeForSend(value: string): string {
  return value.replace(TOKEN_RE, (_m, _source: string, name: string) => `\u0001${name}\u0001`);
}

/** 发送串里的技能名（顺序即出现顺序；重复保留，供气泡兜底用） */
export function skillsInSent(message: string): string[] {
  const names: string[] = [];
  SENT_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = SENT_RE.exec(message))) names.push(m[1]);
  return names;
}

/** 展示用：去掉包裹字符，只留下技能名 */
export function stripSkillMarks(message: string): string {
  return message.replace(SENT_RE, (_m, name: string) => name);
}

/** 已有技能块结束时在新块前补空格，保持技能区连续（顺序即选择顺序） */
function lastTokenEnd(value: string): number {
  let end = -1;
  let m: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(value))) end = m.index + m[0].length;
  return end;
}

/**
 * 插入一个技能块。
 *  · 已经有技能块 → 紧跟在最后一个技能块后面；
 *  · 输入为空 → 技能块放最前；
 *  · leading=true（"/" 触发）→ 技能块放最前，正文接在后面；
 *  · 其余（打字中途选择）→ 接在当前文字后面。
 * 技能块与正文之间都会留一个空格。
 */
export function insertSkillToken(value: string, token: string, leading = false): string {
  const end = lastTokenEnd(value);
  if (end >= 0) {
    return `${value.slice(0, end)} ${token}${value.slice(end)}`;
  }
  const body = value.trim();
  if (!body) return `${token} `;
  return leading ? `${token} ${body}` : `${body} ${token}`;
}

/** 移除全部技能块（切到快速对话时用），并清理多余空格 */
export function removeAllSkillTokens(value: string): string {
  TOKEN_RE.lastIndex = 0;
  return value.replace(TOKEN_RE, "").replace(/ {2,}/g, " ").trimStart();
}
