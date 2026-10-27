/**
 * 工作台：单个文件的只读预览。
 *
 * 文本/代码走 CodeBlock（行号 + 高亮），Markdown 默认渲染并可切源码，
 * 图片走 raw 接口，其余二进制显示占位说明。
 */
import { useEffect, useState } from "react";
import {
  CheckOutlined,
  CopyOutlined,
  FileUnknownOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { Alert, Image, Segmented, Spin, Tooltip } from "antd";
import { fetchWorkspaceFile } from "../api";
import type { WorkspaceFileData } from "../types";
import CodeBlock, { langFromExt } from "./CodeBlock";
import HtmlPreview from "./HtmlPreview";
import Markdown from "./Markdown";
import { BinaryPreview } from "./preview/registry";

interface Props {
  workspaceId: string;
  path: string;
  name: string;
  ext: string;
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function dirOf(path: string): string {
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

/** 复制文本：优先 Clipboard API，失败时退回 textarea + execCommand。 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 走降级 */
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

export default function FilePreview({ workspaceId, path, name, ext }: Props) {
  const [data, setData] = useState<WorkspaceFileData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  const [viewMode, setViewMode] = useState<"preview" | "source">("preview");
  const [copied, setCopied] = useState(false);

  const isMarkdown = [".md", ".markdown"].includes(ext.toLowerCase());
  const isHtml = [".html", ".htm"].includes(ext.toLowerCase());
  const isMhtml = [".mht", ".mhtml"].includes(ext.toLowerCase());
  const canToggle = isMarkdown || isHtml || isMhtml;
  const scriptPreview = isHtml || isMhtml;

  useEffect(() => {
    setLoading(true);
    setError("");
    setViewMode("preview");
    setCopied(false);
    fetchWorkspaceFile(workspaceId, path)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [workspaceId, path, nonce]);

  const copyContent = async () => {
    const text =
      data?.kind === "mhtml" ? data.html || "" : data?.kind === "text" ? data.content || "" : "";
    if (!text) return;
    const ok = await copyText(text);
    if (!ok) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const sourceText = data?.kind === "mhtml" ? data.html || "" : data?.content || "";

  return (
    <div className="wb-file">
      <div className="wb-file-toolbar">
        <span className="wb-file-path" title={path}>
          {path}
        </span>
        <span className="wb-file-meta">
          {data ? `${data.encoding || ""}${data.encoding ? " · " : ""}${formatSize(data.size)}` : ""}
        </span>
        {canToggle && data && (data.kind === "text" || data.kind === "mhtml") && (
          <Segmented
            size="small"
            value={viewMode}
            onChange={(v) => setViewMode(v as "preview" | "source")}
            options={[
              { value: "preview", label: "预览" },
              { value: "source", label: "源码" },
            ]}
          />
        )}
        {scriptPreview && viewMode === "preview" && data && (
          <span className="wb-html-hint">脚本已启用（沙箱隔离）</span>
        )}
        {(data?.kind === "text" || data?.kind === "mhtml") && (
          <Tooltip title={copied ? "已复制" : "复制内容"}>
            <span className="wb-icon-btn" onClick={copyContent}>
              {copied ? <CheckOutlined /> : <CopyOutlined />}
            </span>
          </Tooltip>
        )}
        <Tooltip title="重新加载">
          <ReloadOutlined className="wb-icon-btn" onClick={() => setNonce((n) => n + 1)} />
        </Tooltip>
      </div>

      <Spin spinning={loading}>
        {error && <Alert type="error" showIcon message={error} style={{ margin: 12 }} />}

        {(data?.kind === "text" || data?.kind === "mhtml") && (
          <div className="wb-text">
            {viewMode === "preview" && isMarkdown && data.kind === "text" && (
              <div className="wb-md">
                <Markdown
                  text={data.content || ""}
                  assetBase={{ workspaceId, dir: dirOf(path) }}
                />
              </div>
            )}
            {viewMode === "preview" && isHtml && data.kind === "text" && (
              <HtmlPreview
                html={data.content || ""}
                workspaceId={workspaceId}
                filePath={path}
              />
            )}
            {viewMode === "preview" && isMhtml && data.kind === "mhtml" && (
              <HtmlPreview
                html={data.html || ""}
                workspaceId={workspaceId}
                filePath={path}
                resources={data.resources}
              />
            )}
            {!(viewMode === "preview" && (isMarkdown || isHtml || isMhtml)) && (
              <div className="wb-code">
                <CodeBlock
                  code={sourceText}
                  lang={isMhtml ? "html" : langFromExt(ext)}
                  showLineNumbers
                />
              </div>
            )}
            {data.kind === "text" && data.truncated && (
              <div className="wb-note">
                内容较大，仅显示前 {data.lines ?? 5000} 行 / 1MB
              </div>
            )}
          </div>
        )}

        {data?.kind === "image" && (
          <div className="wb-image-wrap">
            {/* 点击图片进入全屏预览（antd Image 的 lightbox 覆盖全屏，支持缩放/旋转） */}
            <Image
              src={data.url}
              alt={name}
              preview={{ mask: "点击全屏查看" }}
            />
          </div>
        )}

        {/* 二进制类预览（PDF 等）统一交给 registry 按 kind 分发 */}
        {data?.kind === "pdf" && data.url && (
          <BinaryPreview
            kind={data.kind}
            workspaceId={workspaceId}
            path={path}
            name={name}
            size={data.size}
            url={data.url}
          />
        )}

        {(data?.kind === "binary" || data?.kind === "unsupported") && (
          <div className="wb-placeholder">
            <FileUnknownOutlined className="wb-placeholder-icon" />
            <div className="wb-placeholder-title">{name}</div>
            <div className="wb-placeholder-desc">
              {data.reason || "暂不支持预览"}
            </div>
            <div className="wb-placeholder-meta">{formatSize(data.size)}</div>
          </div>
        )}
      </Spin>
    </div>
  );
}
