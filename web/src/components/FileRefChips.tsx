/**
 * 用户消息里的 @ 引用 chip：文件名 + 相对路径，点击用现有原始文件接口打开。
 *
 * 历史回放时批量 stat 一次：文件被删 / 被改只做标注，不重读内容、也不阻断重发——
 * 引用是「发送那一刻的快照」，前端只负责把差异标出来。
 */
import { useEffect, useState } from "react";
import { FileOutlined } from "@ant-design/icons";
import { statWorkspaceFiles, workspaceRawUrl } from "../api";
import type { WorkspaceRef, WorkspaceStatItem } from "../types";

export default function FileRefChips({
  refs,
  workspaceId,
}: {
  refs?: WorkspaceRef[];
  workspaceId: string;
}) {
  const list = refs ?? [];
  const [stat, setStat] = useState<Record<string, WorkspaceStatItem>>({});
  const key = list.map((r) => r.path).join("\n");

  useEffect(() => {
    if (!list.length || !workspaceId) return;
    let cancelled = false;
    statWorkspaceFiles(workspaceId, list.map((r) => r.path))
      .then((items) => {
        if (cancelled) return;
        const map: Record<string, WorkspaceStatItem> = {};
        items.forEach((it) => {
          map[it.path] = it;
        });
        setStat(map);
      })
      .catch(() => {
        /* 标注失败不影响展示 */
      });
    return () => {
      cancelled = true;
    };
    // 只按路径集合重新查询；refs 数组引用变化不重复请求
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, workspaceId]);

  if (!list.length) return null;

  return (
    <div className="msg-refs">
      {list.map((r) => {
        const s = stat[r.path];
        const deleted = !!s && !s.exists;
        const changed =
          !!s &&
          s.exists &&
          ((r.size > 0 && s.size !== r.size) ||
            (r.mtime > 0 && Math.abs(s.mtime - r.mtime) > 1));
        return (
          <span
            key={r.path}
            className={`msg-ref${deleted ? " deleted" : changed ? " changed" : ""}`}
            title={deleted ? `${r.path}（已删除）` : `${r.path}（点击打开）`}
            onClick={() => {
              if (!deleted) {
                window.open(workspaceRawUrl(workspaceId, r.path), "_blank", "noopener");
              }
            }}
          >
            <FileOutlined />
            <span className="msg-ref-name">{r.name || r.path.split("/").pop()}</span>
            {deleted ? (
              <em className="msg-ref-tag">已删除</em>
            ) : changed ? (
              <em className="msg-ref-tag">已变更</em>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}
