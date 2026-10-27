/**
 * PDF 预览：pdf.js 渲染到 canvas，自绘单页工具栏（翻页 + 缩放 + 适应宽度）。
 *
 * 为什么不用 <iframe> 让浏览器自带阅读器渲染：各家内置阅读器行为不一致，
 * 手机上 iframe 内的滚动/缩放很容易失灵；走 canvas 时表现完全由我们控制。
 * pdf.js 用 legacy 构建，避免顶层 await 要求、兼容更多浏览器。
 */
import { useEffect, useRef, useState } from "react";
import {
  LeftOutlined,
  RightOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from "@ant-design/icons";
import { Alert, Spin, Tooltip } from "antd";
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import type { PreviewProps } from "./registry";

/** 缩放的上下限，避免极端值把 canvas 撑爆 */
const MIN_SCALE = 0.3;
const MAX_SCALE = 4;

export default function PdfPreview({ url, name }: PreviewProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [pageNum, setPageNum] = useState(1);
  const [numPages, setNumPages] = useState(0);
  const [scale, setScale] = useState(1);
  const [fitWidth, setFitWidth] = useState(true);
  const [tick, setTick] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // 加载文档：pdf.js 的 worker 与文档实例都要在卸载时销毁，否则切文件会漏内存
  useEffect(() => {
    let alive = true;
    let task: PDFDocumentLoadingTask | null = null;
    setLoading(true);
    setError("");
    setPageNum(1);
    setScale(1);
    setFitWidth(true);
    setDoc(null);

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
        const worker = (await import("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url")).default;
        pdfjs.GlobalWorkerOptions.workerSrc = worker;
        task = pdfjs.getDocument({ url });
        const loaded = await task.promise;
        if (!alive) return;   // 已卸载：清理交给下面的 task.destroy()
        setDoc(loaded);
        setNumPages(loaded.numPages);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();

    return () => {
      alive = false;
      void task?.destroy();
    };
  }, [url]);

  // 抽屉宽度变化时，适应宽度模式要重新排版
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || !fitWidth) return;
    const ro = new ResizeObserver(() => setTick((n) => n + 1));
    ro.observe(stage);
    return () => ro.disconnect();
  }, [fitWidth]);

  // 渲染当前页
  useEffect(() => {
    if (!doc) return;
    let cancelled = false;
    let task: RenderTask | null = null;

    (async () => {
      const page = await doc.getPage(pageNum);
      if (cancelled) return;
      const canvas = canvasRef.current;
      const stage = stageRef.current;
      if (!canvas || !stage) return;

      const base = page.getViewport({ scale: 1 });
      const cssScale = fitWidth
        ? Math.max(MIN_SCALE, (stage.clientWidth - 28) / base.width)
        : scale;
      // 按设备像素比放大画布，再用 CSS 缩回去，文字边缘才不会糊
      const dpr = window.devicePixelRatio || 1;
      const viewport = page.getViewport({ scale: cssScale * dpr });
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.style.width = `${Math.floor(viewport.width / dpr)}px`;
      canvas.style.height = `${Math.floor(viewport.height / dpr)}px`;

      task = page.render({ canvas, viewport });
      try {
        await task.promise;
      } catch {
        // 翻页/缩放时上一帧被取消是正常现象
      }
    })().catch((e) => {
      if (!cancelled) setError(e instanceof Error ? e.message : String(e));
    });

    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, pageNum, scale, fitWidth, tick]);

  const zoom = (delta: number) => {
    setFitWidth(false);
    setScale((s) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s + delta)));
  };

  return (
    <div className="wb-pdf">
      <div className="wb-pdf-toolbar">
        <Tooltip title="上一页">
          <LeftOutlined
            className="wb-icon-btn"
            onClick={() => setPageNum((n) => Math.max(1, n - 1))}
          />
        </Tooltip>
        <span className="wb-pdf-page">
          {pageNum} / {numPages || "-"}
        </span>
        <Tooltip title="下一页">
          <RightOutlined
            className="wb-icon-btn"
            onClick={() => setPageNum((n) => Math.min(numPages || 1, n + 1))}
          />
        </Tooltip>
        <span className="wb-pdf-sep" />
        <Tooltip title="缩小">
          <ZoomOutOutlined className="wb-icon-btn" onClick={() => zoom(-0.2)} />
        </Tooltip>
        <Tooltip title="点击恢复适应宽度">
          <span
            className="wb-pdf-zoom"
            onClick={() => {
              setFitWidth(true);
              setScale(1);
            }}
          >
            {fitWidth ? "适应宽度" : `${Math.round(scale * 100)}%`}
          </span>
        </Tooltip>
        <Tooltip title="放大">
          <ZoomInOutlined className="wb-icon-btn" onClick={() => zoom(0.2)} />
        </Tooltip>
      </div>

      <div className="wb-pdf-stage" ref={stageRef}>
        {error && (
          <Alert
            type="error"
            showIcon
            message={`PDF 加载失败：${name}`}
            description={error}
            style={{ margin: 12, textAlign: "left" }}
          />
        )}
        <Spin spinning={loading}>
          <canvas ref={canvasRef} className="wb-pdf-canvas" />
        </Spin>
      </div>
    </div>
  );
}
