/**
 * 富渲染 Markdown：
 *  · GFM（表格/删除线/任务列表）+ KaTeX 数学公式（$...$ / $$...$$）
 *  · 原生 HTML（rehype-raw），经 rehype-sanitize 白名单消毒（禁 script/事件/js: 链接）
 *  · ```echarts / ```mermaid / ```svg 代码块 → 渲染为真实图形；失败时退回代码块并提示
 */
import {
  isValidElement,
  lazy,
  memo,
  Suspense,
  useEffect,
  useRef,
  useState,
  useCallback,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Dropdown, Image, message, Tooltip } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  CopyOutlined,
  DownloadOutlined,
  FullscreenOutlined,
  LoadingOutlined,
  MinusOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeKatex from "rehype-katex";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import "katex/dist/katex.min.css";
import CodeBlock from "./CodeBlock";

// echarts 体积大（gzip 约 200KB），而绝大多数消息里没有图表：
// 懒加载后只有真正出现 ```echarts 时才拉取，不占首屏。
const Chart = lazy(() => import("./Chart"));
// mermaid 同样很大（含布局引擎），只在真的出现 ```mermaid 时加载
const Mermaid = lazy(() => import("./Mermaid"));

/** 走图形渲染的代码块语言：识别到就交给专用组件，而不是语法高亮 */
const DIAGRAM_LANGS = new Set(["echarts", "mermaid", "svg"]);

// 消毒白名单：在 GitHub 默认 schema 之上，
// 放行 mark 高亮标签、KaTeX 渲染所需的 class/span 样式与代码块的 language- 标记
const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), "mark"],
  attributes: {
    ...defaultSchema.attributes,
    code: [...(defaultSchema.attributes?.code ?? []), ["className", /^language-[\w-]+$/]],
    span: [...(defaultSchema.attributes?.span ?? []), ["className", /^katex[\w -]*$/], "style"],
    div: [...(defaultSchema.attributes?.div ?? []), ["className", /^katex[\w -]*$/]],
  },
};

/** ```echarts 代码块：JSON → 图表；解析失败展示错误 + 原文 */
function EchartsBlock({ source }: { source: string }) {
  let option: Record<string, unknown>;
  try {
    option = JSON.parse(source);
  } catch (e) {
    return (
      <div className="echarts-error">
        <div className="echarts-error-title">ECharts 配置不是合法 JSON：{String(e)}</div>
        <div className="md-pre">
          <CodeBlock code={source} lang="json" />
        </div>
      </div>
    );
  }
  return (
    <Suspense fallback={<div className="echarts-box" />}>
      <Chart option={option} />
    </Suspense>
  );
}

/** 复制文本：优先 Clipboard API，失败时退回 textarea + execCommand。 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 无权限 / 无用户手势 → 走降级 */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** 一次性下载链接（Blob URL 用完即释放，不依赖后端） */
function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** mermaid 输出的 SVG 常带 width="100%" / max-width 样式，位图化前先按 viewBox 定成像素尺寸 */
function sizedSvg(svg: string): { text: string; w: number; h: number } {
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  const el = doc.documentElement;
  let w = 0;
  let h = 0;
  const vb = el.getAttribute("viewBox");
  if (vb) {
    const p = vb.split(/[\s,]+/).map(Number);
    w = p[2];
    h = p[3];
  }
  if (!Number.isFinite(w) || !Number.isFinite(h) || !w || !h) {
    w = Number.parseFloat(el.getAttribute("width") || "") || 800;
    h = Number.parseFloat(el.getAttribute("height") || "") || 600;
  }
  el.setAttribute("width", String(w));
  el.setAttribute("height", String(h));
  el.removeAttribute("style");
  return { text: new XMLSerializer().serializeToString(el), w, h };
}

/** SVG 文本 → PNG Blob：画到 canvas 再导出（2 倍尺寸，透明底补白）。
 *  注意：带 foreignObject / 外部引用的 SVG 会被浏览器判定污染，toBlob 抛 SecurityError，
 *  由调用方兜住并提示用户。 */
async function svgToPng(svgText: string): Promise<Blob> {
  const { text, w, h } = sizedSvg(svgText);
  const scale = 2;
  const url = URL.createObjectURL(new Blob([text], { type: "image/svg+xml;charset=utf-8" }));
  try {
    // 注意用 window.Image：本文件里的 Image 是 antd 组件，不是 DOM 构造函数
    const img = new window.Image();
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error("SVG 解码失败"));
      img.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(w * scale));
    canvas.height = Math.max(1, Math.round(h * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("当前浏览器不支持 canvas");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("PNG 编码失败");
    return blob;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** 图形视口：按住拖动平移，右下角三个按钮依次是复位 / 放大 / 缩小。
 *  只用 pointer 事件（鼠标 / 触控 / 触控笔一套逻辑），拖动时不选中文字。
 *  「复位」和首次显示都是「缩放到完整可见并居中」：图再高也不会被视口裁掉。 */
function DiagramViewport({
  children,
  allowUpscale = false,
}: {
  children: ReactNode;
  /** 全屏预览时允许放大填满视口；卡片内保持 1:1，不把小图撑大 */
  allowUpscale?: boolean;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [dragging, setDragging] = useState(false);
  // 当前缩放值同步到 ref：fit 需要按已缩放的测量值反推真实尺寸
  const viewRef = useRef(view);
  useEffect(() => {
    viewRef.current = view;
  });

  const fit = useCallback(() => {
    const box = boxRef.current;
    const stage = stageRef.current;
    if (!box || !stage) return;
    // 量的是「图」本身（svg / img）而不是外层包裹层：包裹层宽度永远是满宽，量不出图的真实大小
    const inner = (stage.querySelector("svg, img") as Element | null) ?? stage;
    const k0 = viewRef.current.k || 1;
    const ir = inner.getBoundingClientRect();
    const sr = stage.getBoundingClientRect();
    // getBoundingClientRect 带着当前 transform，除以 k0 才是布局尺寸；偏移同理
    const cw = Math.max(1, ir.width / k0);
    const ch = Math.max(1, ir.height / k0);
    const ox = (ir.left - sr.left) / k0;
    const oy = (ir.top - sr.top) / k0;
    const availW = Math.max(1, box.clientWidth);
    const availH = Math.max(1, box.clientHeight);
    const k = Math.min(allowUpscale ? 3 : 1, availW / cw, availH / ch);
    setView({
      k,
      x: (availW - cw * k) / 2 - ox * k,
      y: (availH - ch * k) / 2 - oy * k,
    });
  }, [allowUpscale]);

  // 图是异步渲染出来的（mermaid）、也可能随窗口宽度重新排版，所以监听尺寸变化后重新适配
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    fit();
    const ro = new ResizeObserver(() => fit());
    ro.observe(stage);
    return () => ro.disconnect();
  }, [fit]);

  // 以某个点为锚点缩放：锚点（鼠标位置 / 视口中心）在缩放前后保持不动，视觉上不会“跳”
  const zoomAt = (factor: number, cx: number, cy: number) =>
    setView((v) => {
      const k = Math.min(4, Math.max(0.3, v.k * factor));
      const ratio = k / v.k;
      return { k, x: cx - (cx - v.x) * ratio, y: cy - (cy - v.y) * ratio };
    });

  const zoom = (factor: number) =>
    zoomAt(factor, (boxRef.current?.clientWidth ?? 0) / 2, (boxRef.current?.clientHeight ?? 0) / 2);

  // 滚轮缩放：必须用原生非 passive 监听才能 preventDefault（React 的 onWheel 是 passive 的，
  // 拦不住页面滚动），否则滚轮一动整页跟着滚。
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = box.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      // 以鼠标位置为锚点，一格滚轮约 ±20%，触控板的小增量也能平滑缩放
      const delta = Math.max(-120, Math.min(120, e.deltaY));
      const factor = Math.exp(-delta * 0.0018);
      setView((v) => {
        const k = Math.min(4, Math.max(0.3, v.k * factor));
        const ratio = k / v.k;
        return { k, x: cx - (cx - v.x) * ratio, y: cy - (cy - v.y) * ratio };
      });
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, []);

  const endDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };

  return (
    <div
      ref={boxRef}
      className={`md-viewport${dragging ? " dragging" : ""}`}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        // 点在右下角的按钮上时不要开始拖拽
        if ((e.target as HTMLElement).closest(".md-viewport-tools")) return;
        dragRef.current = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y };
        setDragging(true);
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const d = dragRef.current;
        if (!d) return;
        setView((v) => ({ ...v, x: d.ox + (e.clientX - d.sx), y: d.oy + (e.clientY - d.sy) }));
      }}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    >
      <div
        ref={stageRef}
        className="md-viewport-stage"
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}
      >
        {children}
      </div>
      <div className="md-viewport-tools">
        <Tooltip title="复位">
          <button type="button" aria-label="复位" onClick={fit}>
            <ReloadOutlined />
          </button>
        </Tooltip>
        <Tooltip title="放大">
          <button type="button" aria-label="放大" onClick={() => zoom(1.2)}>
            <PlusOutlined />
          </button>
        </Tooltip>
        <Tooltip title="缩小">
          <button type="button" aria-label="缩小" onClick={() => zoom(1 / 1.2)}>
            <MinusOutlined />
          </button>
        </Tooltip>
      </div>
    </div>
  );
}

/** 流式生成中的占位：先把代码块外壳摆出来，白蒙版写「绘图生成中…」，
 *  不让用户盯着不断增长的 SVG / mermaid 源码等，等生成完再一次性渲染成图。 */
function DiagramPending({ lang }: { lang: string }) {
  return (
    <div className="md-code-card">
      <div className="md-code-head">
        <span className="md-code-lang">{lang}</span>
      </div>
      <div className="md-code-body md-code-figure">
        <div className="md-pending">
          <div className="md-pending-mask">
            <LoadingOutlined spin />
            <span>{lang === "echarts" ? "图表生成中…" : "绘图生成中…"}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 图形代码块外壳：左上角语言名，右上角「视图 / 代码」切换 + 复制 + 下载（SVG / PNG）。
 *  mermaid 与 svg 共用这一套交互，区别只在 view 里放什么、矢量文本从哪来。 */
function DiagramCard({
  lang,
  source,
  view,
  ready,
  svgText,
  pngSvg,
}: {
  lang: "mermaid" | "svg";
  source: string;
  /** 视图区内容（mermaid 渲染结果 / SVG 图） */
  view: ReactNode;
  /** 矢量图是否已就绪：未就绪时下载按钮置灰 */
  ready: boolean;
  /** 下载 .svg 用的矢量文本 */
  svgText: () => string;
  /** 导出 PNG 用的矢量文本（mermaid 需要按纯文本标签重新渲染一次） */
  pngSvg: () => Promise<string>;
}) {
  const [mode, setMode] = useState<"view" | "code">("view");
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  // 全屏预览层的位置与尺寸：铺满「对话区域」而不是整个浏览器
  const [fsRect, setFsRect] = useState<
    { top: number; left: number; width: number; height: number } | null
  >(null);
  const timerRef = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  // 全屏预览的覆盖范围：对话消息区 + 下方输入区（底部取输入框的下沿）
  const measureHost = useCallback(() => {
    const scroll = document.querySelector(".chat-scroll") as HTMLElement | null;
    const composer = document.querySelector(".composer") as HTMLElement | null;
    const s = scroll?.getBoundingClientRect();
    if (!s) {
      return { top: 0, left: 0, width: window.innerWidth, height: window.innerHeight };
    }
    const bottom = composer ? composer.getBoundingClientRect().bottom : s.bottom;
    return { top: s.top, left: s.left, width: s.width, height: Math.max(1, bottom - s.top) };
  }, []);

  const openFullscreen = useCallback(() => setFsRect(measureHost()), [measureHost]);

  // 全屏预览打开时：Esc 关闭，窗口尺寸变化后重新贴合对话区域
  useEffect(() => {
    if (!fsRect) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFsRect(null);
    };
    const onResize = () => setFsRect(measureHost());
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    };
  }, [fsRect, measureHost]);

  const onCopy = async () => {
    if (!(await copyText(source))) return;
    setCopied(true);
    window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 1500);
  };

  const onDownload = async (kind: "svg" | "png") => {
    if (busy || !ready) return;
    setBusy(true);
    try {
      if (kind === "svg") {
        saveBlob(new Blob([svgText()], { type: "image/svg+xml;charset=utf-8" }), `${lang}-diagram.svg`);
      } else {
        saveBlob(await svgToPng(await pngSvg()), `${lang}-diagram.png`);
      }
    } catch (e) {
      // 以前这里只 console.warn，用户看到的就是「点了没反应」，所以改成页面提示
      message.error(`导出 ${kind.toUpperCase()} 失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="md-code-card">
      <div className="md-code-head">
        <span className="md-code-lang">{lang}</span>
        <span className="md-code-actions">
          <span className="md-code-toggle">
            <button
              type="button"
              className={mode === "view" ? "on" : ""}
              onClick={() => setMode("view")}
            >
              视图
            </button>
            <button
              type="button"
              className={mode === "code" ? "on" : ""}
              onClick={() => setMode("code")}
            >
              代码
            </button>
          </span>
          <Tooltip title={copied ? "已复制" : "复制源码"}>
            <button
              type="button"
              className={`md-code-icon${copied ? " on" : ""}`}
              onClick={onCopy}
              aria-label="复制源码"
            >
              {copied ? <CheckOutlined /> : <CopyOutlined />}
            </button>
          </Tooltip>
          <Dropdown
            trigger={["click"]}
            placement="bottomRight"
            disabled={!ready || busy}
            menu={{
              items: [
                { key: "svg", label: "下载 SVG" },
                { key: "png", label: "下载 PNG" },
              ],
              onClick: ({ key }) => void onDownload(key === "png" ? "png" : "svg"),
            }}
          >
            <button
              type="button"
              className="md-code-icon"
              disabled={!ready || busy}
              aria-label="下载图"
            >
              {busy ? <LoadingOutlined /> : <DownloadOutlined />}
            </button>
          </Dropdown>
          <Tooltip title="全屏预览">
            <button
              type="button"
              className="md-code-icon"
              disabled={!ready}
              onClick={openFullscreen}
              aria-label="全屏预览"
            >
              <FullscreenOutlined />
            </button>
          </Tooltip>
        </span>
      </div>
      {mode === "code" ? (
        <div className="md-code-body md-code-source">
          <CodeBlock code={source} showLineNumbers />
        </div>
      ) : (
        <div className="md-code-body md-code-figure">
          <DiagramViewport>{view}</DiagramViewport>
        </div>
      )}
      {/* 全屏预览：铺满对话区域（不是整个浏览器），图同样可拖动 / 滚轮缩放 / 复位 */}
      {fsRect &&
        createPortal(
          <div
            className="md-fs-overlay"
            style={{
              top: fsRect.top,
              left: fsRect.left,
              width: fsRect.width,
              height: fsRect.height,
            }}
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setFsRect(null);
            }}
          >
            <div className="md-fs">
              <div className="md-fs-bar">
                <span className="md-code-lang">{lang}</span>
                <Tooltip title="关闭（Esc）">
                  <button
                    type="button"
                    className="md-code-icon"
                    onClick={() => setFsRect(null)}
                    aria-label="关闭全屏预览"
                  >
                    <CloseOutlined />
                  </button>
                </Tooltip>
              </div>
              <div className="md-fs-body">
                <DiagramViewport allowUpscale>{view}</DiagramViewport>
              </div>
            </div>
          </div>,
          document.body
        )}
    </div>
  );
}

/** ```mermaid 代码块：默认展示渲染好的图，点「代码」切回源码。 */
function MermaidBlock({ source }: { source: string }) {
  // mermaid 渲染出的 SVG：下载按钮要用它，图还没渲染好前按钮置灰
  const [svg, setSvg] = useState("");
  return (
    <DiagramCard
      lang="mermaid"
      source={source}
      ready={!!svg}
      svgText={() => svg}
      // 屏幕上那张 SVG 的标签是 foreignObject，直接位图化会被浏览器判定污染
      // （toBlob 抛 SecurityError），所以导出 PNG 前按纯文本标签重新渲染一次
      pngSvg={() => import("./Mermaid").then((m) => m.renderForExport(source))}
      view={
        <Suspense fallback={<div className="md-diagram-tip">正在渲染图表…</div>}>
          <Mermaid source={source} onSvg={setSvg} />
        </Suspense>
      }
    />
  );
}

/** ```svg 代码块：源码本身就是 SVG，装成 data URI 交给 <img> 渲染。
 *  img 上下文里的 SVG 不执行脚本，比放开 rehype-sanitize 的 svg 标签白名单安全得多。
 *  内容里找不到 <svg> 时退回普通代码块，避免出现一张空白图。 */
function SvgBlock({ source }: { source: string }) {
  // 模型常在围栏里顺带 <?xml ?> 声明、注释或说明文字：只截取 <svg>…</svg>，
  // 否则内容不是以 "<" 开始（或末尾多出文字）时整块会退化成纯代码块 / 破图
  const raw = source.trim();
  const start = raw.toLowerCase().indexOf("<svg");
  const end = raw.toLowerCase().lastIndexOf("</svg>");
  const svg = start >= 0 ? raw.slice(start, end >= 0 ? end + 6 : undefined) : "";
  if (!svg) {
    return (
      <div className="md-pre">
        <CodeBlock code={source} lang="svg" />
      </div>
    );
  }
  return (
    <DiagramCard
      lang="svg"
      source={source}
      ready
      svgText={() => svg}
      pngSvg={() => Promise.resolve(svg)}
      view={
        <img
          className="md-svg"
          src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
          alt="SVG 图"
          draggable={false}
        />
      }
    />
  );
}

/** 递归从 React children 里提取纯文本（处理字符串/数字/数组/嵌套元素） */
function extractText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (isValidElement(node)) return extractText((node.props as { children?: unknown }).children);
  return "";
}

/** 把相对路径（可含 ../）拼到基准目录上，用于工作台里的 Markdown 图片。 */
function joinRel(dir: string, src: string): string {
  const out: string[] = [];
  for (const part of `${dir ? dir + "/" : ""}${src}`.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return out.join("/");
}

function Markdown({
  text,
  workspaceId,
  assetBase,
  pending = false,
}: {
  text: string;
  workspaceId?: string;
  /** 工作台文件预览 / 对话内图片：把 Markdown 里的相对图片路径映射到当前工作区 */
  assetBase?: { workspaceId: string; dir: string };
  /** 仍在流式输出：代码块可能还没闭合，先按源码展示，结束再渲染图形 */
  pending?: boolean;
}) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRaw, rehypeKatex, [rehypeSanitize, sanitizeSchema]]}
        components={{
          code({ className, children }) {
            const lang = /language-(\w+)/.exec(className || "")?.[1];
            if (!lang || !DIAGRAM_LANGS.has(lang)) {
              return <code className={className}>{children}</code>;
            }
            // 流式输出中：只摆外壳 + 白蒙版，别让用户盯着半截源码
            if (pending) {
              return <DiagramPending lang={lang} />;
            }
            const source = String(children).trim();
            if (lang === "mermaid") {
              return <MermaidBlock source={source} />;
            }
            return (
              <div className={`md-diagram md-diagram-${lang}`}>
                {lang === "echarts" ? (
                  <EchartsBlock source={source} />
                ) : (
                  <SvgBlock source={source} />
                )}
              </div>
            );
          },
          pre({ children }) {
            if (isValidElement(children)) {
              const props = children.props as { className?: string; children?: unknown };
              const lang = /language-([\w-]+)/.exec(props.className || "")?.[1];
              // 图形块：把原始 <code> 交回渲染，由 code 组件产出图形（流式时是占位蒙版）。
              // 注意 pre 拿到的 children 是「还没经过 code 组件」的元素，className 仍是
              // language-xxx，不能靠子元素上的 md-diagram 标记判断（那样永远不成立）。
              if (lang && DIAGRAM_LANGS.has(lang)) {
                return <>{children}</>;
              }
              // 普通代码块：从内嵌 <code> 元素提取语言与文本，交由 CodeBlock 做语法高亮
              const text = extractText(props.children);
              return (
                <div className="md-pre">
                  {lang && <div className="md-pre-lang">{lang}</div>}
                  <CodeBlock code={text} lang={lang} />
                </div>
              );
            }
            return <pre>{children}</pre>;
          },
          img({ src, alt }) {
            // 本地附件路径 → 走后端 /api/attachment 读取（优先于工作区相对路径）
            let finalSrc = src || "";
            if (workspaceId && finalSrc.startsWith(".attachments/")) {
              finalSrc = `/api/attachment?path=${encodeURIComponent(finalSrc)}&workspace=${encodeURIComponent(workspaceId)}`;
            } else if (assetBase && finalSrc && !/^(?:[a-z][a-z0-9+.-]*:|\/|#)/i.test(finalSrc)) {
              // 相对图片（工作台按当前目录、对话按工作区根）：映射到 /api/workspace/raw
              const rel = joinRel(assetBase.dir, finalSrc);
              finalSrc =
                `/api/workspace/raw?workspace_id=${encodeURIComponent(assetBase.workspaceId)}` +
                `&path=${encodeURIComponent(rel)}`;
            }
            // 统一用 antd Image：正文里的图也能点开缩放 / 全屏浏览
            return (
              <Image
                className="md-img"
                src={finalSrc}
                alt={alt || ""}
                style={{ maxWidth: "100%", borderRadius: 8 }}
                preview={{ mask: false }}
              />
            );
          },
          a({ href, children, ...props }) {
            // 正文里的超链接一律新标签页打开：当前页面是正在进行的对话，
            // 直接跳走会把会话界面顶掉（noopener 同时断掉新页面对本页的 window.opener）
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                {children}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/**
 * 消息列表会随输入、流式增量频繁重渲染：Markdown 解析（remark/rehype/KaTeX）
 * 是这里最贵的一步，按 text 记忆化，未变化的旧消息直接跳过解析。
 */
export default memo(Markdown);
