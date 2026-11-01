/**
 * ```mermaid 代码块：把源码交给 mermaid 渲染成 SVG。
 *
 *  · 懒加载：mermaid（含布局引擎）体积很大，只有消息里真出现 ```mermaid 时才拉取；
 *  · securityLevel: "strict"：mermaid 会对标签文本做转义 / 清洗，不执行用户可控脚本；
 *  · 语法错误不抛给用户看整段堆栈：回退成带语言标记的代码块 + 一行错误摘要。
 */
import { useEffect, useState } from "react";
import CodeBlock from "./CodeBlock";

let loader: Promise<typeof import("mermaid")> | null = null;

function loadMermaid() {
  if (!loader) {
    loader = import("mermaid").then((mod) => {
      mod.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "default",
        fontFamily: "inherit",
      });
      return mod;
    });
  }
  return loader;
}

let seq = 0;

//: 导出专用 init 指令：关掉 HTML 标签（foreignObject）。带 foreignObject 的 SVG
//: 画到 canvas 会被浏览器标记为污染，toBlob 直接抛 SecurityError，导不出 PNG。
//: 必须写顶层 htmlLabels —— mermaid 读取时是 `config.htmlLabels ?? flowchart.htmlLabels ?? true`，
//: 只写 flowchart 子项会被顶层的默认值 true 盖掉（之前的坑就在这里）。
const EXPORT_INIT = '%%{init: {"htmlLabels": false}}%%\n';

/** 导出 PNG 用：按「纯 SVG 文本标签」重新渲染一次源码，得到可安全位图化的 SVG。 */
export async function renderForExport(source: string): Promise<string> {
  const mod = await loadMermaid();
  const res = await mod.default.render(`mmd-export-${++seq}`, EXPORT_INIT + source);
  return res.svg;
}

/** mermaid 的错误信息通常很长，只取第一行，避免撑破气泡 */
function briefError(e: unknown): string {
  const text = String((e as Error)?.message || e || "未知错误");
  return text.split("\n").find((line) => line.trim())?.slice(0, 200) || "未知错误";
}

export default function Mermaid({
  source,
  onSvg,
}: {
  source: string;
  /** 渲染成功时把 SVG 字符串回传给外层（下载按钮要用） */
  onSvg?: (svg: string) => void;
}) {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    setSvg("");
    setError("");
    onSvg?.("");
    // 每次渲染用独立 id：StrictMode 会双跑 effect，共用 id 会撞车
    const id = `mmd-${++seq}`;
    loadMermaid()
      .then((mod) => mod.default.render(id, source))
      .then((res) => {
        if (alive) {
          setSvg(res.svg);
          onSvg?.(res.svg);
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(briefError(e));
      });
    return () => {
      alive = false;
    };
  }, [source, onSvg]);

  if (error) {
    return (
      <div className="md-diagram-error">
        <div className="md-diagram-error-title">Mermaid 渲染失败：{error}</div>
        <div className="md-pre">
          <CodeBlock code={source} lang="mermaid" />
        </div>
      </div>
    );
  }
  if (!svg) return <div className="md-diagram-tip">正在渲染图表…</div>;
  return <div className="mermaid-box" dangerouslySetInnerHTML={{ __html: svg }} />;
}
