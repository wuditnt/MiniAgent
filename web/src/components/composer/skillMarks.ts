/**
 * 输入框里「技能文字块」的编码约定。
 *
 * 输入框的值始终是一个纯字符串（Sender 是受控的），技能块用一个不可见的
 * 私有字符包裹：`\u0001skill:<名字>\u0001`。内容可编辑区把它渲染成不可编辑的
 * 蓝色块，用户看到的是名字；提交前用 parseComposer 把标记拆成「技能名 + 正文」。
 */
const MARK = "\u0001";
const TOKEN_RE = /\u0001skill:([^\u0001]*)\u0001/g;

/** 把技能名编码成输入串里的标记 */
export function skillToken(name: string): string {
  return `${MARK}skill:${name}${MARK}`;
}

/** 拆出技能名与用户正文（正文只去掉标记，不 trim —— 交给调用方决定） */
export function parseComposer(value: string): { skills: string[]; text: string } {
  const skills: string[] = [];
  const text = value.replace(TOKEN_RE, (_m, name: string) => {
    skills.push(name);
    return "";
  });
  return { skills, text };
}

/** 当前输入串里是否含技能块 */
export function hasSkillToken(value: string): boolean {
  return value.includes(`${MARK}skill:`);
}

/** 最后一个技能块结束的位置（没有技能块时返回 -1） */
function lastTokenEnd(value: string): number {
  let end = -1;
  let m: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(value))) end = m.index + m[0].length;
  return end;
}

/**
 * 插入一个技能块。
 *  · 已经有技能块 → 紧跟在最后一个技能块后面，保持技能区连续、顺序即选择顺序；
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

/** 移除指定技能块，并清理因此产生的多余空格 */
export function removeSkillToken(value: string, name: string): string {
  const token = skillToken(name);
  const next = value.replace(`${token} `, "").replace(token, "");
  return next.replace(/ {2,}/g, " ").trimStart();
}
