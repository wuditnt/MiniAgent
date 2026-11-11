/**
 * 工作区文件「拖拽成 @ 引用」的跨组件协议。
 *
 * 工作台文件树拖起时写入自定义 MIME，对话区在 dragover / drop 里识别它，
 * 把文件转成输入框里的引用 chip。刻意不复用 text/plain：
 * 拖一段普通文字进来不应该被当成引用。
 */
import type { WorkspaceSearchItem } from "../../types";

/** 自定义 MIME：与系统文件拖拽（Files）、纯文本拖拽区分开 */
export const REF_DND_MIME = "application/x-miniagent-ref";

/** 拖拽载荷：WorkspaceEntry 里构成一次引用所需的字段 */
export type DraggedRef = Pick<
  WorkspaceSearchItem,
  "name" | "path" | "size" | "mtime" | "ext"
>;

/** 拖起时写入载荷；effectAllowed=copy 让光标显示为「复制」 */
export function setDraggedRef(dt: DataTransfer, ref: DraggedRef): void {
  dt.setData(REF_DND_MIME, JSON.stringify(ref));
  dt.effectAllowed = "copy";
}

/**
 * dragenter / dragover 阶段读不到载荷（浏览器保护），只能看类型；
 * 这两个阶段用它决定「要不要接管这次拖拽并画提示」。
 */
export function hasDraggedRef(dt: DataTransfer | null): boolean {
  return Array.from(dt?.types ?? []).includes(REF_DND_MIME);
}

/** drop 阶段解析载荷；格式不对一律当没拖，避免脏数据插进输入框 */
export function getDraggedRef(dt: DataTransfer | null): DraggedRef | null {
  const raw = dt?.getData(REF_DND_MIME);
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as Partial<DraggedRef>;
    if (typeof v.path !== "string" || !v.path) return null;
    return {
      name: typeof v.name === "string" && v.name ? v.name : v.path.split("/").pop() || v.path,
      path: v.path,
      size: typeof v.size === "number" ? v.size : 0,
      mtime: typeof v.mtime === "number" ? v.mtime : 0,
      ext: typeof v.ext === "string" ? v.ext : "",
    };
  } catch {
    return null;
  }
}
