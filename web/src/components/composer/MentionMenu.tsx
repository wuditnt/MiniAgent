/**
 * "@ 文件引用" 面板：工作区搜索结果 / 最近使用 / 从本地上传。
 *
 * 键盘导航（↑↓ / Enter / Tab / Esc）由 Chat 统一接管：面板只负责展示与
 * 把「当前候选列表」上报给上层，避免输入框、面板两套焦点互相抢。
 */
import { useEffect, useState } from "react";
import { FileOutlined, FileImageOutlined, PaperClipOutlined } from "@ant-design/icons";
import { searchWorkspaceFiles } from "../../api";
import type { WorkspaceSearchItem } from "../../types";

function fmtSize(n: number): string {
  if (!n) return "";
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`;
  return `${(n / (1024 * 1024)).toFixed(1)}M`;
}

function splitPath(path: string): { dir: string; name: string } {
  const i = path.lastIndexOf("/");
  return i < 0 ? { dir: "", name: path } : { dir: path.slice(0, i), name: path.slice(i + 1) };
}

export default function MentionMenu({
  open,
  workspaceId,
  query,
  items,
  active,
  recents,
  uploading,
  remote,
  onItems,
  onPick,
  onUpload,
}: {
  open: boolean;
  workspaceId: string;
  query: string;
  /** 当前候选（由本组件搜索后回传，切换查询时同步更新） */
  items: WorkspaceSearchItem[];
  active: number;
  recents: WorkspaceSearchItem[];
  uploading: boolean;
  /** 远程只读：工作区引用可用，但本地上传被禁止 */
  remote?: boolean;
  onItems: (items: WorkspaceSearchItem[]) => void;
  onPick: (item: WorkspaceSearchItem) => void;
  onUpload: () => void;
}) {
  const [searching, setSearching] = useState(false);

  // 只有输入了查询词才请求服务端；空查询直接用「最近使用」
  useEffect(() => {
    if (!open) return;
    if (!query.trim()) {
      onItems(recents);
      setSearching(false);
      return;
    }
    setSearching(true);
    let cancelled = false;
    const timer = window.setTimeout(() => {
      searchWorkspaceFiles(workspaceId, query, 20)
        .then((data) => {
          if (!cancelled) onItems(data.items);
        })
        .catch(() => {
          if (!cancelled) onItems([]);
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 150);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // recents 只在打开面板 / 清空查询时使用，不参与依赖，避免每次选择都重查
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, query, workspaceId]);

  useEffect(() => {
    if (!open) setSearching(false);
  }, [open]);

  if (!open) return null;

  const list = items;
  const title = query.trim() ? "工作区文件" : "最近使用";

  return (
    <div className="mention-menu" role="listbox" onMouseDown={(e) => e.preventDefault()}>
      {searching && !list.length ? (
        <div className="mention-empty">搜索中…</div>
      ) : list.length ? (
        <>
          <div className="mention-group">{title}</div>
          {list.map((it, i) => {
            const { dir } = splitPath(it.path);
            const isImg = /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(it.name);
            return (
              <div
                key={it.path}
                className={`mention-item${i === active ? " active" : ""}`}
                role="option"
                aria-selected={i === active}
                onClick={() => onPick(it)}
              >
                <span className="mention-icon">
                  {isImg ? <FileImageOutlined /> : <FileOutlined />}
                </span>
                <span className="mention-main">
                  <span className="mention-name">{it.name}</span>
                  {dir && <span className="mention-dir">{dir}</span>}
                </span>
                <span className="mention-size">{fmtSize(it.size)}</span>
              </div>
            );
          })}
        </>
      ) : query.trim() ? (
        <div className="mention-empty">没有匹配的文件</div>
      ) : null}

      <div className="mention-group mention-actions">
        <div
          className={`mention-item mention-upload${remote ? " disabled" : ""}`}
          onClick={() => {
            if (remote) return;
            onUpload();
          }}
          title={remote ? "远程访问不支持上传附件" : "选择本地图片 / 文件上传"}
        >
          <span className="mention-icon">
            <PaperClipOutlined />
          </span>
          <span className="mention-main">
            <span className="mention-name">
              {uploading ? "上传中…" : "从本地上传…"}
            </span>
            <span className="mention-dir">
              {remote ? "远程只读，已禁用" : "图片走多模态，文本注入正文"}
            </span>
          </span>
        </div>
      </div>
      <span className="mention-hint">↑↓ 选择 · Enter 插入 · Esc 关闭</span>
    </div>
  );
}
