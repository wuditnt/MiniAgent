/**
 * 输入框的「斜杠快捷指令」层。
 *
 * "/" 不是普通字符，而是「给 AI 下达快捷指令」的触发符。这里把它单独抽出来：
 * 每个指令声明自己的触发字符与判定规则，输入层只负责把「这次输入是否触发了
 * 哪条指令、触发在哪个位置」告诉上层。
 *
 * 扩展方式：往 SLASH_COMMANDS 里加一条（例如 "/clear"、"/summarize"），
 * 在 detect 里写清楚触发条件；Chat 侧按 command.id 决定弹哪个面板即可。
 */

export interface SlashCommand {
  /** 指令标识，上层用它决定打开哪个选择器 / 执行什么动作 */
  id: string;
  /** 触发字符，目前只有 "/" */
  trigger: string;
  /** 人类可读名称（将来可用于命令提示） */
  label: string;
  /**
   * 判断这次输入是否触发了本指令。
   * @param prev 上一次的输入值
   * @param next 这一次的输入值
   * @returns 触发字符在新值里的下标；-1 表示不触发
   */
  detect: (prev: string, next: string) => number;
}

export interface SlashHit {
  command: SlashCommand;
  /** 触发字符在输入串里的位置 */
  index: number;
}

/** 找出这次输入新增的字符位置（只处理「净增一个字符」的情况） */
function insertedIndex(prev: string, next: string): number {
  const max = Math.min(prev.length, next.length);
  let i = 0;
  while (i < max && prev[i] === next[i]) i++;
  return i;
}

/** 触发符必须位于开头或空白之后 —— 直接接在文字后面（如 abc/）按普通字符处理 */
function atBoundary(text: string, index: number): boolean {
  return index === 0 || /\s/.test(text[index - 1]);
}

/** 技能选择：行首或空格后的 "/" */
export const SKILL_COMMAND: SlashCommand = {
  id: "skill",
  trigger: "/",
  label: "选择技能",
  detect(prev, next) {
    if (next.length !== prev.length + 1) return -1;
    const at = insertedIndex(prev, next);
    if (next[at] !== this.trigger) return -1;
    return atBoundary(next, at) ? at : -1;
  },
};

/** 全部已注册的斜杠指令（顺序即优先级） */
export const SLASH_COMMANDS: SlashCommand[] = [SKILL_COMMAND];

/** 检测这次输入命中的斜杠指令 */
export function detectSlashCommand(prev: string, next: string): SlashHit | null {
  for (const command of SLASH_COMMANDS) {
    const index = command.detect(prev, next);
    if (index >= 0) return { command, index };
  }
  return null;
}

/** 删掉指定位置的触发字符（选中技能后调用，不影响其它字符） */
export function stripTrigger(value: string, index: number, trigger = "/"): string {
  if (value[index] !== trigger) return value;
  return value.slice(0, index) + value.slice(index + 1);
}

/** 取触发符之后、当前行结束前的查询词（用于技能模糊搜索） */
export function skillQuery(value: string, index: number): string {
  if (value[index] !== "/") return "";
  const nl = value.indexOf("\n", index);
  const end = nl < 0 ? value.length : nl;
  return value.slice(index + 1, end).trim();
}

/**
 * 删掉「触发符 + 本行查询词」，保留行尾空白与后续行。
 * 选中技能后用它替换，避免只删 "/" 而把查询词留在输入框里。
 */
export function stripSkillCommand(value: string, index: number): string {
  if (value[index] !== "/") return value;
  const nl = value.indexOf("\n", index);
  const end = nl < 0 ? value.length : nl;
  let cut = end;
  while (cut > index + 1 && /\s/.test(value[cut - 1])) cut--;
  return value.slice(0, index) + value.slice(cut);
}
