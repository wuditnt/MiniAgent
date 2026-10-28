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
