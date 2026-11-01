/**
 * 工作台：单个目录的文件列表页。
 *
 * 只做单层浏览：点目录进入下一层，点文件交给 Workbench 开标签。
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
import { Empty, message, Modal, Spin, Tooltip } from "antd";
import { deleteWorkspaceEntry, fetchWorkspaceDir, workspaceDownloadUrl } from "../api";
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

  const segments = (data?.path || "").split("/").filter(Boolean);
  const rootName = (data?.root || "").split(/[\\/]/).filter(Boolean).pop() || data?.root || "";

  return (
    <div className="wb-files">
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
              <div
                className={`wb-row ${entry.type}`}
                key={entry.path}
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
            ))}
          </div>
        )}
        {data?.truncated && <div className="wb-note">仅显示前 500 项</div>}
      </Spin>
    </div>
  );
}
