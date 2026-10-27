/**
 * CSV / TSV 表格预览。
 *
 * 10M 个格子不可能一次性塞进浏览器，所以后端按「行窗口 × 列窗口」分页，
 * 这里只渲染当前窗口（默认 200 行 × 50 列），左右/上下翻页时再取下一页。
 */
import { useCallback, useEffect, useState } from "react";
import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import { Alert, Spin, Tooltip } from "antd";
import { fetchWorkspaceTable } from "../../api";
import type { WorkspaceTablePage } from "../../types";
import type { PreviewProps } from "./registry";

/** 与后端 _TABLE_PAGE_ROWS / _TABLE_PAGE_COLS 保持一致 */
const ROW_PAGE = 200;
const COL_PAGE = 50;

export default function TablePreview({ workspaceId, path, data }: PreviewProps) {
  const [page, setPage] = useState<WorkspaceTablePage | null>(data.table ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(
    async (rowOffset: number, colOffset: number) => {
      setLoading(true);
      setError("");
      try {
        setPage(
          await fetchWorkspaceTable(workspaceId, path, {
            rowOffset,
            rowLimit: ROW_PAGE,
            colOffset,
            colLimit: COL_PAGE,
          }),
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [workspaceId, path],
  );

  // 首屏数据随 /api/workspace/file 一起返回；缺失时补拉第一页
  useEffect(() => {
    if (!data.table) void load(0, 0);
  }, [data.table, load]);

  if (!page) {
    return <div className="wb-preview-loading">{error || "正在解析表格…"}</div>;
  }

  const rowStart = page.rows.length ? page.row_offset + 1 : 0;
  const rowEnd = page.row_offset + page.rows.length;
  const colStart = page.columns.length ? page.col_offset + 1 : 0;
  const colEnd = page.col_offset + page.columns.length;
  const canPrevRow = page.row_offset > 0;
  const canNextRow = rowEnd < page.total_rows;
  const canPrevCol = page.col_offset > 0;
  const canNextCol = colEnd < page.total_cols;

  const cls = (enabled: boolean) => `wb-icon-btn${enabled ? "" : " disabled"}`;

  return (
    <div className="wb-table">
      <div className="wb-table-toolbar">
        <Tooltip title="上一页">
          <LeftOutlined
            className={cls(canPrevRow)}
            onClick={() => canPrevRow && void load(Math.max(0, page.row_offset - ROW_PAGE), page.col_offset)}
          />
        </Tooltip>
        <span className="wb-table-range">
          {rowStart}-{rowEnd} / {page.total_rows} 行
        </span>
        <Tooltip title="下一页">
          <RightOutlined
            className={cls(canNextRow)}
            onClick={() => canNextRow && void load(page.row_offset + ROW_PAGE, page.col_offset)}
          />
        </Tooltip>

        <span className="wb-pdf-sep" />

        <Tooltip title="左移一屏列">
          <LeftOutlined
            className={cls(canPrevCol)}
            onClick={() => canPrevCol && void load(page.row_offset, Math.max(0, page.col_offset - COL_PAGE))}
          />
        </Tooltip>
        <span className="wb-table-range">
          {colStart}-{colEnd} / {page.total_cols} 列
        </span>
        <Tooltip title="右移一屏列">
          <RightOutlined
            className={cls(canNextCol)}
            onClick={() => canNextCol && void load(page.row_offset, page.col_offset + COL_PAGE)}
          />
        </Tooltip>
      </div>

      {error && <Alert type="error" showIcon message={error} style={{ margin: 12 }} />}

      <Spin spinning={loading}>
        <div className="wb-table-stage">
          {page.columns.length === 0 ? (
            <div className="wb-note">这个文件没有可展示的列</div>
          ) : (
            <table className="wb-table-grid">
              <thead>
                <tr>
                  <th className="wb-table-idx">#</th>
                  {page.columns.map((col, i) => (
                    <th key={i} title={col}>
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {page.rows.map((row, r) => (
                  <tr key={r}>
                    <td className="wb-table-idx">{page.row_offset + r + 1}</td>
                    {row.map((cell, c) => (
                      <td key={c} title={cell}>
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {page.rows.length === 0 && <div className="wb-note">这个文件没有数据行</div>}
        </div>
      </Spin>

      {page.truncated && (
        <div className="wb-note">超出解析上限（10000 行 / 1000 列 / 20MB），后续内容未展示</div>
      )}
    </div>
  );
}
