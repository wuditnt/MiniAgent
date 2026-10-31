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
  LeftOutlined,
  ReloadOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { Alert, Image, Segmented, Spin, Tooltip } from "antd";
import { fetchWorkspaceDir, fetchWorkspaceFile, workspaceRawUrl } from "../api";
import type { WorkspaceEntry, WorkspaceFileData } from "../types";
import CodeBlock, { langFromExt } from "./CodeBlock";
import HtmlPreview from "./HtmlPreview";
import Markdown from "./Markdown";
import { PreviewByKind } from "./preview/registry";

interface Props {
  workspaceId: string;
  path: string;
  name: string;
  ext: string;
  /** 翻页：交给工作台替换当前标签（同目录图片之间切换） */
  onNavigate?: (entry: WorkspaceEntry) => void;
}

/** 与后端 _IMAGE_EXTS 保持一致，用于同目录翻页时的图片筛选 */
const IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"];

/** 同目录图片清单缓存：翻页会重挂载组件，按工作区 + 目录复用一次请求 */
const dirCache = new Map<string, WorkspaceEntry[]>();

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

export default function FilePreview({ workspaceId, path, name, ext, onNavigate }: Props) {
  const [data, setData] = useState<WorkspaceFileData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  const [viewMode, setViewMode] = useState<"preview" | "source">("preview");
  const [copied, setCopied] = useState(false);
  const [images, setImages] = useState<WorkspaceEntry[] | null>(null);
  const [imageIndex, setImageIndex] = useState(-1);

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

  // 图片：载入同目录图片清单并算出当前序号；翻页由工作台替换标签
  useEffect(() => {
    if (data?.kind !== "image") {
      setImages(null);
      setImageIndex(-1);
      return;
    }
    const dir = dirOf(path);
    const key = `${workspaceId}:${dir}`;
    let alive = true;
    const apply = (entries: WorkspaceEntry[]) => {
      if (!alive) return;
      const list = entries
        .filter((e) => e.type === "file" && IMAGE_EXTS.includes(e.ext.toLowerCase()))
        .sort((a, b) => a.name.localeCompare(b.name, "zh", { numeric: true }));
      setImages(list);
      setImageIndex(list.findIndex((e) => e.path === path));
    };
    const cached = dirCache.get(key);
    if (cached) {
      apply(cached);
    } else {
      fetchWorkspaceDir(workspaceId, dir)
        .then((d) => {
          dirCache.set(key, d.entries);
          apply(d.entries);
        })
        .catch(() => {
          if (alive) {
            setImages([]);
            setImageIndex(-1);
          }
        });
    }
    return () => {
      alive = false;
    };
  }, [workspaceId, path, data]);

  // 预加载相邻两张，翻页时不出现空白等待
  useEffect(() => {
    if (!images || imageIndex < 0) return;
    [images[imageIndex - 1], images[imageIndex + 1]].forEach((e) => {
      if (!e) return;
      const img = new window.Image();
      img.src = workspaceRawUrl(workspaceId, e.path);
    });
  }, [images, imageIndex, workspaceId]);

  const canPrev = !!images && imageIndex > 0;
  const canNext = !!images && imageIndex >= 0 && imageIndex < images.length - 1;

  const goImage = (delta: number) => {
    if (!images) return;
    const next = images[imageIndex + delta];
    if (next) onNavigate?.(next);
  };

  // 面板内方向键翻页；全屏预览与输入框交给各自处理
  useEffect(() => {
    if (data?.kind !== "image" || !onNavigate) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (document.querySelector(".ant-image-preview-root")) return;
      const el = document.activeElement as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      e.preventDefault();
      goImage(e.key === "ArrowLeft" ? -1 : 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

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
          <span className="wb-file-path-text">{path}</span>
          {data && <span className="wb-file-size">{formatSize(data.size)}</span>}
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
        {data?.kind === "image" && images && images.length > 1 && (
          <span className="wb-img-nav">
            <span
              className={`wb-icon-btn ${canPrev ? "" : "disabled"}`}
              title={canPrev ? "上一张" : "已经是第一张"}
              aria-label="上一张"
              onClick={() => goImage(-1)}
            >
              <LeftOutlined />
            </span>
            <span className="wb-img-counter">
              {imageIndex >= 0 ? imageIndex + 1 : "?"} / {images.length}
            </span>
            <span
              className={`wb-icon-btn ${canNext ? "" : "disabled"}`}
              title={canNext ? "下一张" : "已经是最后一张"}
              aria-label="下一张"
              onClick={() => goImage(1)}
            >
              <RightOutlined />
            </span>
          </span>
        )}
        <Tooltip title="重新加载">
          <ReloadOutlined
            className="wb-icon-btn"
            onClick={() => {
              dirCache.clear();
              setNonce((n) => n + 1);
            }}
          />
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
                内容较大，已截断（最多 {data.lines ?? 5000} 行 / 20MB；单行上限 4000 字符）
              </div>
            )}
          </div>
        )}

        {data?.kind === "image" && (
          <div className="wb-image-wrap">
            {/* 全屏预览带上同目录全部图片，进入后可连续左右切换 */}
            <Image.PreviewGroup
              items={
                images?.length
                  ? images.map((e) =>
                      e.path === path ? data.url || "" : workspaceRawUrl(workspaceId, e.path)
                    )
                  : [data.url || ""]
              }
            >
              <Image
                src={data.url}
                alt={name}
                preview={{ mask: "点击全屏查看" }}
              />
            </Image.PreviewGroup>
          </div>
        )}

        {/* PDF / CSV 表格等结构化预览统一交给 registry 按 kind 分发 */}
        {(data?.kind === "pdf" || data?.kind === "table") && (
          <PreviewByKind
            kind={data.kind}
            workspaceId={workspaceId}
            path={path}
            name={name}
            size={data.size}
            data={data}
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
