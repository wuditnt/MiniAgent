/**
 * 二进制文件的预览注册表。
 *
 * 后端 /api/workspace/file 返回 kind（image / pdf / …）+ 原始字节地址 url，
 * 前端按 kind 查表拿组件。新增一种格式只需要：
 *   1. 后端 _BINARY_PREVIEWS 登记扩展名 → kind；
 *   2. 这里加一行映射，组件自然走懒加载。
 * 预览实现与 FilePreview 主流程完全解耦：组件只依赖 PreviewProps。
 */
import { lazy, Suspense, type ComponentType } from "react";

export interface PreviewProps {
  workspaceId: string;
  /** 工作区内相对路径 */
  path: string;
  name: string;
  size: number;
  /** 后端 raw 接口地址（同源，自动携带登录 Cookie） */
  url: string;
}

/** kind → 预览组件。全部懒加载，不打开对应文件就不下载代码。 */
const PREVIEWS: Record<string, ComponentType<PreviewProps>> = {
  pdf: lazy(() => import("./PdfPreview")),
};

export function BinaryPreview({ kind, ...props }: PreviewProps & { kind: string }) {
  const Component = PREVIEWS[kind];
  if (!Component) return null;
  return (
    <Suspense fallback={<div className="wb-preview-loading">正在加载预览组件…</div>}>
      <Component {...props} />
    </Suspense>
  );
}
