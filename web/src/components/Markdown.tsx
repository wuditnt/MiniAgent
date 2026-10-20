/**
 * 富渲染 Markdown：
 *  · GFM（表格/删除线/任务列表）+ KaTeX 数学公式（$...$ / $$...$$）
 *  · 原生 HTML（rehype-raw），经 rehype-sanitize 白名单消毒（禁 script/事件/js: 链接）
 *  · ```echarts 代码块 → 渲染为真实图表；JSON 不合法时退回代码块并提示
 */
import { isValidElement, memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeKatex from "rehype-katex";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import "katex/dist/katex.min.css";
import Chart from "./Chart";
import CodeBlock from "./CodeBlock";

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
  return <Chart option={option} />;
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
}: {
  text: string;
  workspaceId?: string;
  /** 工作台文件预览：把 Markdown 里的相对图片路径映射到当前工作区 */
  assetBase?: { workspaceId: string; dir: string };
}) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRaw, rehypeKatex, [rehypeSanitize, sanitizeSchema]]}
        components={{
          code({ className, children }) {
            const lang = /language-(\w+)/.exec(className || "")?.[1];
            if (lang === "echarts") {
              return <EchartsBlock source={String(children).trim()} />;
            }
            return <code className={className}>{children}</code>;
          },
          pre({ children }) {
            // 图表块不再套 <pre> 外壳，直接渲染
            if (
              isValidElement(children) &&
              /language-echarts/.test(
                (children.props as { className?: string }).className || "",
              )
            ) {
              return <>{children}</>;
            }
            // 普通代码块：从内嵌 <code> 元素提取语言与文本，交由 CodeBlock 做语法高亮
            if (isValidElement(children)) {
              const props = children.props as { className?: string; children?: unknown };
              const lang = /language-([\w-]+)/.exec(props.className || "")?.[1];
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
          img({ src, alt, ...props }) {
            // 本地附件路径 → 走后端 /api/attachment 读取
            let finalSrc = src || "";
            if (assetBase && finalSrc && !/^(?:[a-z][a-z0-9+.-]*:|\/|#)/i.test(finalSrc)) {
              // 工作台里的相对图片：映射到 /api/workspace/raw
              const rel = joinRel(assetBase.dir, finalSrc);
              finalSrc =
                `/api/workspace/raw?workspace_id=${encodeURIComponent(assetBase.workspaceId)}` +
                `&path=${encodeURIComponent(rel)}`;
            } else if (workspaceId && finalSrc.startsWith(".attachments/")) {
              finalSrc = `/api/attachment?path=${encodeURIComponent(finalSrc)}&workspace=${encodeURIComponent(workspaceId)}`;
            }
            return <img src={finalSrc} alt={alt || ""} {...props} style={{ maxWidth: "100%", borderRadius: 8 }} />;
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
