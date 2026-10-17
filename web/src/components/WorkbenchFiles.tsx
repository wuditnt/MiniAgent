/**
 * 工作台：单个目录的文件列表页。
 *
 * 只做单层浏览：点目录进入下一层，点文件交给 Workbench 开标签。
 */
import { useCallback, useEffect, useState } from "react";
import {
  FileImageOutlined,
  FileMarkdownOutlined,
  FileTextOutlined,
  FolderOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { Empty, Spin, Tooltip } from "antd";
import { fetchWorkspaceDir } from "../api";
import type { WorkspaceDirData, WorkspaceEntry } from "../types";

interface Props {
  workspaceId: string;
  path: string;
  onPathChange: (path: string) => void;
  onOpenFile: (entry: WorkspaceEntry) => void;
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

export default function WorkbenchFiles({ workspaceId, path, onPathChange, onOpenFile }: Props) {
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
                <span className="wb-row-name" title={entry.name}>
                  {entry.name}
                </span>
                {entry.type === "file" && (
                  <span className="wb-row-meta">{formatSize(entry.size)}</span>
                )}
              </div>
            ))}
          </div>
        )}
        {data?.truncated && <div className="wb-note">仅显示前 500 项</div>}
      </Spin>
    </div>
  );
}
