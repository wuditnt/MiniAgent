/**
 * 文件预览注册表。
 *
 * 后端 /api/workspace/file 给出 kind（image / pdf / table / …）与文件描述，
 * 这里按 kind 查表拿组件。新增一种格式只需要：
 *   1. 后端 _BINARY_PREVIEWS 或表格分支登记扩展名 → kind；
 *   2. 这里加一行映射，组件自然走懒加载。
 * 预览实现与 FilePreview 主流程完全解耦：组件只依赖 PreviewProps。
 */
import { lazy, Suspense, type ComponentType } from "react";
import type { WorkspaceFileData } from "../../types";

export interface PreviewProps {
  workspaceId: string;
  /** 工作区内相对路径 */
  path: string;
  name: string;
  size: number;
  /** 后端 /api/workspace/file 的完整描述（含 url / table 等） */
  data: WorkspaceFileData;
}

/** kind → 预览组件。全部懒加载，不打开对应文件就不下载代码。 */
const PREVIEWS: Record<string, ComponentType<PreviewProps>> = {
  pdf: lazy(() => import("./PdfPreview")),
  table: lazy(() => import("./TablePreview")),
};

export function PreviewByKind({ kind, ...props }: PreviewProps & { kind: string }) {
  const Component = PREVIEWS[kind];
  if (!Component) return null;
  return (
    <Suspense fallback={<div className="wb-preview-loading">正在加载预览组件…</div>}>
      <Component {...props} />
    </Suspense>
  );
}
