/**
 * 输入框里「@ 文件引用」的编码与触发规则。
 *
 * 与 skillMarks 用同一套思路但互不冲突：
 *  · 技能块：\u0002skill:<来源>:<名字>\u0002
 *  · 文件块：\u0003file:<encodeURIComponent(相对路径)>\u0003
 * 文件块只存在于输入框内部；发送时从正文剥离，引用走独立的 references 字段。
 */

/** 输入框内部的文件块标记前缀 */
const FILE_PREFIX = "\u0003file:";
const MFILE_RE = /\u0003file:([^\u0003]*)\u0003/g;

/** 编码一个文件块标记（路径可能含空格 / 中文 / #，必须整体编码） */
export function fileToken(path: string): string {
  return `${FILE_PREFIX}${encodeURIComponent(path)}\u0003`;
}

/** 输入框内部值 → 引用路径列表（按出现顺序，重复保留） */
export function filesInValue(value: string): string[] {
  const out: string[] = [];
  MFILE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = MFILE_RE.exec(value))) {
    try {
      out.push(decodeURIComponent(m[1]));
    } catch {
      out.push(m[1]);
    }
  }
  return out;
}

/** 发送前剥离文件块标记，只留下用户正文 */
export function stripFileMarks(value: string): string {
  MFILE_RE.lastIndex = 0;
  return value.replace(MFILE_RE, "").replace(/ {2,}/g, " ");
}

/** 找出这次输入新增字符的位置（只处理净增一个字符） */
function insertedIndex(prev: string, next: string): number {
  const max = Math.min(prev.length, next.length);
  let i = 0;
  while (i < max && prev[i] === next[i]) i++;
  return i;
}

/** 触发符必须位于开头或空白之后（abc@ 按普通字符处理） */
function atBoundary(text: string, index: number): boolean {
  return index === 0 || /\s/.test(text[index - 1]);
}

/** 检测这次输入是否打出了 "@"；返回它在输入串里的下标，-1 = 不触发 */
export function detectMention(prev: string, next: string): number {
  if (next.length !== prev.length + 1) return -1;
  const at = insertedIndex(prev, next);
  if (next[at] !== "@") return -1;
  return atBoundary(next, at) ? at : -1;
}

/** "@" 之后、下一个空白 / 换行之前的查询词 */
export function mentionQuery(value: string, index: number): string {
  if (value[index] !== "@") return "";
  let end = index + 1;
  while (end < value.length && !/\s/.test(value[end])) end++;
  return value.slice(index + 1, end);
}

/**
 * 把「@ + 查询词」替换成文件块。
 * 前后的空白保持原样；查询词后面没有空白时补一个，方便继续打字。
 */
export function replaceMentionWithToken(
  value: string,
  index: number,
  token: string
): string {
  if (value[index] !== "@") return value;
  let end = index + 1;
  while (end < value.length && !/\s/.test(value[end])) end++;
  const before = value.slice(0, index);
  const after = value.slice(end);
  const tail = after ? (/^\s/.test(after) ? after : ` ${after}`) : " ";
  return `${before}${token}${tail}`;
}

/** 输入框里已选文件路径（去重，保持出现顺序） */
export function uniqueFiles(value: string): string[] {
  return [...new Set(filesInValue(value))];
}
