/**
 * 轻量代码高亮块：react-syntax-highlighter（PrismLight 内核）按需注册语言，
 * 只打包含注册的几个语言，确认弹窗 / 工具参数等场景共用。
 *
 * 语言选择：优先按参数语义（code/command/sql），其次按文件扩展名推断，
 * 推断不出时不传 language，Prism 原样输出纯文本（等同普通 pre）。
 */
import { PrismLight } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import powershell from "react-syntax-highlighter/dist/esm/languages/prism/powershell";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";

PrismLight.registerLanguage("python", python);
PrismLight.registerLanguage("powershell", powershell);
PrismLight.registerLanguage("bash", bash);
PrismLight.registerLanguage("shell", bash);
PrismLight.registerLanguage("sql", sql);
PrismLight.registerLanguage("json", json);
PrismLight.registerLanguage("javascript", javascript);
PrismLight.registerLanguage("js", javascript);
PrismLight.registerLanguage("typescript", typescript);
PrismLight.registerLanguage("ts", typescript);
PrismLight.registerLanguage("html", markup);
PrismLight.registerLanguage("markup", markup);
PrismLight.registerLanguage("css", css);
PrismLight.registerLanguage("yaml", yaml);
PrismLight.registerLanguage("markdown", markdown);

/** 扩展名 → Prism 语言 */
const EXT_LANG: Record<string, string> = {
  py: "python", ps1: "powershell", sh: "bash", bat: "bash", cmd: "bash",
  sql: "sql", json: "json", js: "javascript", mjs: "javascript",
  ts: "typescript", tsx: "typescript", jsx: "javascript",
  html: "html", htm: "html", xml: "markup", svg: "markup",
  css: "css", yml: "yaml", yaml: "yaml", md: "markdown",
};

/** 按文件扩展名推断高亮语言（工作台文件预览用）。 */
export function langFromExt(ext: string): string | undefined {
  return EXT_LANG[String(ext || "").replace(/^\./, "").toLowerCase()];
}

/** 参数语义优先：code/command/sql；path 类参数用扩展名兜底。 */
export function guessLang(tool: string | undefined, key: string, value: string): string | undefined {
  if (key === "code") return tool === "run_shell" ? "powershell" : "python";
  if (key === "command") return "powershell";
  if (key === "sql") return "sql";
  if (/^(path|file|target|output)$/i.test(key) || /\.(\w+)$/.test(value)) {
    const ext = /\.(\w+)$/.exec(value)?.[1]?.toLowerCase();
    if (ext && EXT_LANG[ext]) return EXT_LANG[ext];
  }
  return undefined;
}

interface Props {
  code: string;
  lang?: string;
  maxHeight?: number;
  /** 工作台文件预览：显示行号 */
  showLineNumbers?: boolean;
}

export default function CodeBlock({ code, lang, maxHeight, showLineNumbers }: Props) {
  const style: React.CSSProperties = {
    background: "transparent",
    margin: 0,
    padding: 0,
    fontSize: 12,
    lineHeight: 1.6,
    whiteSpace: "pre",
    overflowX: "auto",
  };
  if (maxHeight) {
    style.maxHeight = maxHeight;
    style.overflowY = "auto";
  }
  return (
    <PrismLight
      language={lang}
      style={oneLight}
      customStyle={style}
      showLineNumbers={showLineNumbers}
      lineNumberStyle={{
        minWidth: "2.6em",
        paddingRight: "1em",
        color: "#bbb",
        userSelect: "none",
      }}
      codeTagProps={{ style: { fontFamily: "ui-monospace, Consolas, monospace" } }}
    >
      {code}
    </PrismLight>
  );
}
