/**
 * 富渲染 Markdown：
 *  · GFM（表格/删除线/任务列表）+ KaTeX 数学公式（$...$ / $$...$$）
 *  · 原生 HTML（rehype-raw），经 rehype-sanitize 白名单消毒（禁 script/事件/js: 链接）
 *  · ```echarts 代码块 → 渲染为真实图表；JSON 不合法时退回代码块并提示
 */
import { isValidElement } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeKatex from "rehype-katex";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import "katex/dist/katex.min.css";
import Chart from "./Chart";

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
        <pre>
          <code>{source}</code>
        </pre>
      </div>
    );
  }
  return <Chart option={option} />;
}

export default function Markdown({ text }: { text: string }) {
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
            return <pre>{children}</pre>;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
