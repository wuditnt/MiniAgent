/**
 * 工作台：单个目录的文件列表页。
 *
 * 只做单层浏览：点目录进入下一层，点文件交给 Workbench 开标签。
 * 右键菜单 = 左键能力的快捷入口；「打开所在文件夹」只在桌面端渲染，
 * 由后端 /api/workspace/reveal 调资源管理器（浏览器/手机端没有这个能力）。
 */
import { useCallback, useEffect, useState } from "react";
import {
  DeleteOutlined,
  DownloadOutlined,
  FileImageOutlined,
  FileMarkdownOutlined,
  FileTextOutlined,
  FolderOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { Dropdown, Empty, message, Modal, Spin, Tooltip } from "antd";
import type { MenuProps } from "antd";
import {
  deleteWorkspaceEntry,
  fetchWorkspaceDir,
  revealWorkspaceEntry,
  workspaceDownloadUrl,
} from "../api";
import type { WorkspaceDirData, WorkspaceEntry } from "../types";

interface Props {
  workspaceId: string;
  path: string;
  onPathChange: (path: string) => void;
  onOpenFile: (entry: WorkspaceEntry) => void;
  /** 删除成功后通知外层：关掉指向它的已打开标签 */
  onDeleted?: (path: string) => void;
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function iconFor(entry: WorkspaceEntry) {
  if (entry.type === "dir") return <FolderOutlined />;
  if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"].includes(entry.ext)) {
    return <FileImageOutlined />;
  }
  if ([".md", ".markdown"].includes(entry.ext)) return <FileMarkdownOutlined />;
  return <FileTextOutlined />;
}

/** 复制文本：局域网 http 不是安全上下文，clipboard 不可用时退回 execCommand。 */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok;
  }
}

export default function WorkbenchFiles({
  workspaceId,
  path,
  onPathChange,
  onOpenFile,
  onDeleted,
}: Props) {
  const [data, setData] = useState<WorkspaceDirData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  // 空白区右键菜单的锚点（行菜单由 antd Dropdown 自己定位）
  const [blank, setBlank] = useState<{ x: number; y: number } | null>(null);

  const isDesktop = !!window.__DESKTOP__;

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    fetchWorkspaceDir(workspaceId, path)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [workspaceId, path]);

  useEffect(() => {
    load();
  }, [load, nonce]);

  // 空白菜单打开后，点击/滚动/改窗口大小都关闭，避免菜单悬空
  useEffect(() => {
    if (!blank) return;
    const close = () => setBlank(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [blank]);

  /** 删除前二次确认：目录会连同内容一起删，且不可恢复 */
  const confirmDelete = (entry: WorkspaceEntry) => {
    const isDir = entry.type === "dir";
    Modal.confirm({
      title: isDir ? "删除这个文件夹？" : "删除这个文件？",
      content: (
        <div>
          <div style={{ wordBreak: "break-all" }}>{entry.path}</div>
          <div style={{ color: "#999", fontSize: 12, marginTop: 6 }}>
            {isDir ? "文件夹内的所有内容都会一起删除，" : ""}删除后无法恢复。
          </div>
        </div>
      ),
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        await deleteWorkspaceEntry(workspaceId, entry.path);
        message.success(`已删除 ${entry.name}`);
        onDeleted?.(entry.path);
        setNonce((n) => n + 1);
      },
    });
  };

  const download = (entry: WorkspaceEntry) => {
    const a = document.createElement("a");
    a.href = workspaceDownloadUrl(workspaceId, entry.path);
    a.download = entry.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const copyPath = async (rel: string) => {
    if (await copyText(rel)) message.success("已复制相对路径");
    else message.error("复制失败，请手动选择路径");
  };

  const reveal = async (rel: string) => {
    try {
      await revealWorkspaceEntry(workspaceId, rel);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
      setNonce((n) => n + 1); // 文件可能已在资源管理器里被移走，刷新列表
    }
  };

  const entryMenu = (entry: WorkspaceEntry): MenuProps => {
    const items: MenuProps["items"] = [{ key: "open", label: "打开" }];
    if (entry.type === "file") items.push({ key: "download", label: "下载" });
    items.push({ key: "copy", label: "复制相对路径" });
    if (isDesktop) items.push({ key: "reveal", label: "打开所在文件夹" });
    items.push({ type: "divider" }, { key: "delete", label: "删除", danger: true });
    return {
      items,
      onClick: ({ key, domEvent }) => {
        domEvent.stopPropagation();
        if (key === "open") {
          entry.type === "dir" ? onPathChange(entry.path) : onOpenFile(entry);
        } else if (key === "download") {
          download(entry);
        } else if (key === "copy") {
          void copyPath(entry.path);
        } else if (key === "reveal") {
          void reveal(entry.path);
        } else if (key === "delete") {
          confirmDelete(entry);
        }
      },
    };
  };

  const blankMenu: MenuProps = {
    items: [
      { key: "refresh", label: "刷新" },
      ...(isDesktop ? [{ key: "reveal", label: "打开当前文件夹" }] : []),
    ],
    onClick: ({ key, domEvent }) => {
      domEvent.stopPropagation();
      setBlank(null);
      if (key === "refresh") setNonce((n) => n + 1);
      else if (key === "reveal") void reveal(path);
    },
  };

  const segments = (data?.path || "").split("/").filter(Boolean);
  const rootName = (data?.root || "").split(/[\\/]/).filter(Boolean).pop() || data?.root || "";

  return (
    <div
      className="wb-files"
      onContextMenu={(e) => {
        // 行菜单交给各自的 Dropdown；这里只处理空白区，并统一压掉浏览器菜单
        e.preventDefault();
        const el = e.target as HTMLElement;
        if (el.closest(".wb-row") || el.closest(".wb-pathbar")) return;
        setBlank({ x: e.clientX, y: e.clientY });
      }}
    >
      <div className="wb-pathbar">
        <div className="wb-breadcrumb">
          <span className="wb-crumb" onClick={() => onPathChange("")} title={data?.root || ""}>
            {rootName || "工作区"}
          </span>
          {segments.map((seg, i) => {
            const target = segments.slice(0, i + 1).join("/");
            return (
              <span className="wb-crumb-wrap" key={target}>
                <span className="wb-crumb-sep">/</span>
                <span className="wb-crumb" onClick={() => onPathChange(target)}>
                  {seg}
                </span>
              </span>
            );
          })}
        </div>
        <Tooltip title="刷新">
          <ReloadOutlined className="wb-icon-btn" onClick={() => setNonce((n) => n + 1)} />
        </Tooltip>
      </div>

      <Spin spinning={loading}>
        {error && <div className="wb-error">{error}</div>}
        {!error && data && data.entries.length === 0 && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这个目录是空的" />
        )}
        {!error && data && data.entries.length > 0 && (
          <div className="wb-list">
            {data.entries.map((entry) => (
              <Dropdown key={entry.path} trigger={["contextMenu"]} menu={entryMenu(entry)}>
                <div
                  className={`wb-row ${entry.type}`}
                  role="button"
                  tabIndex={0}
                  onClick={() =>
                    entry.type === "dir" ? onPathChange(entry.path) : onOpenFile(entry)
                  }
                  onKeyDown={(e) => {
                    if (e.key !== "Enter" && e.key !== " ") return;
                    entry.type === "dir" ? onPathChange(entry.path) : onOpenFile(entry);
                  }}
                >
                  <span className="wb-row-icon">{iconFor(entry)}</span>
                  <span className="wb-row-main">
                    <span className="wb-row-name" title={entry.name}>
                      {entry.name}
                    </span>
                    {entry.type === "file" && (
                      <span className="wb-row-meta">{formatSize(entry.size)}</span>
                    )}
                  </span>
                  {entry.type === "file" && (
                    <Tooltip title="下载">
                      <a
                        className="wb-row-dl"
                        href={workspaceDownloadUrl(workspaceId, entry.path)}
                        download={entry.name}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <DownloadOutlined />
                      </a>
                    </Tooltip>
                  )}
                  <Tooltip title="删除">
                    <span
                      className="wb-row-del"
                      role="button"
                      aria-label={`删除 ${entry.name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        confirmDelete(entry);
                      }}
                    >
                      <DeleteOutlined />
                    </span>
                  </Tooltip>
                </div>
              </Dropdown>
            ))}
          </div>
        )}
        {data?.truncated && <div className="wb-note">仅显示前 500 项</div>}
      </Spin>

      {blank && (
        <Dropdown
          open
          trigger={[]}
          menu={blankMenu}
          onOpenChange={(open) => {
            if (!open) setBlank(null);
          }}
        >
          <span
            style={{ position: "fixed", left: blank.x, top: blank.y, width: 0, height: 0 }}
          />
        </Dropdown>
      )}
    </div>
  );
}
