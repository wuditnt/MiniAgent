/**
 * 右侧工作台抽屉：顶部标签页 + 内容区。
 *
 * 状态约定：
 *  · 没有标签 → 显示「开始」页；
 *  · 「文件」标签承载工作区目录浏览；
 *  · 每个文件一个标签，同一路径只开一个；
 *  · 全部标签关闭 → 通知父组件收起抽屉，并清空状态（下次打开回到开始页）；
 *  · 手动收起 → 保留标签，再次展开继续原标签。
 *
 * 交互：
 *  · 左边缘可拖拽调宽，宽度记在 localStorage；
 *  · 右上角全屏按钮让抽屉占满主区（保留左侧导航栏），再次点击还原。
 */
import { useEffect, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  CloseOutlined,
  CodeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  GlobalOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { Tooltip } from "antd";
import type { WorkspaceEntry } from "../types";
import FilePreview from "./FilePreview";
import WorkbenchFiles from "./WorkbenchFiles";

interface Props {
  open: boolean;
  workspaceId: string;
  onClose: () => void;
  onFullscreenChange?: (fullscreen: boolean) => void;
}

type Tab =
  | { id: "files"; kind: "files"; title: string }
  | { id: string; kind: "file"; title: string; path: string; ext: string };

const WIDTH_KEY = "miniagent.workbenchWidth";
const MIN_WIDTH = 320;
const MAX_WIDTH = 900;
const DEFAULT_WIDTH = 420;

function loadWidth(): number {
  const raw = Number(localStorage.getItem(WIDTH_KEY));
  if (!Number.isFinite(raw) || raw < MIN_WIDTH) return DEFAULT_WIDTH;
  return Math.min(raw, MAX_WIDTH);
}

export default function Workbench({
  open,
  workspaceId,
  onClose,
  onFullscreenChange,
}: Props) {
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState("");
  const [dir, setDir] = useState("");
  const [width, setWidth] = useState(loadWidth);
  const [resizing, setResizing] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  // 切换工作区后，旧标签指向的文件已不属于当前边界，直接回到开始页
  useEffect(() => {
    setTabs([]);
    setActive("");
    setDir("");
  }, [workspaceId]);

  // 外部收起抽屉时同步退出全屏，避免主区一直隐藏
  useEffect(() => {
    if (!open && fullscreen) {
      setFullscreen(false);
      onFullscreenChange?.(false);
    }
  }, [open, fullscreen, onFullscreenChange]);

  const closeWorkbench = () => {
    if (fullscreen) {
      setFullscreen(false);
      onFullscreenChange?.(false);
    }
    onClose();
  };

  const toggleFullscreen = () => {
    const next = !fullscreen;
    setFullscreen(next);
    onFullscreenChange?.(next);
  };

  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    setResizing(true);

    const onMove = (ev: MouseEvent) => {
      const max = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, window.innerWidth - 320));
      const next = Math.min(max, Math.max(MIN_WIDTH, startWidth - (ev.clientX - startX)));
      setWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      setWidth((w) => {
        localStorage.setItem(WIDTH_KEY, String(Math.round(w)));
        return w;
      });
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const openFiles = () => {
    setTabs((prev) =>
      prev.some((t) => t.kind === "files")
        ? prev
        : [...prev, { id: "files", kind: "files", title: "文件" }]
    );
    setActive("files");
  };

  const openFile = (entry: WorkspaceEntry) => {
    const id = `file:${entry.path}`;
    setTabs((prev) =>
      prev.some((t) => t.id === id)
        ? prev
        : [...prev, { id, kind: "file", title: entry.name, path: entry.path, ext: entry.ext }]
    );
    setActive(id);
  };

  const closeTab = (id: string) => {
    const idx = tabs.findIndex((t) => t.id === id);
    const next = tabs.filter((t) => t.id !== id);
    setTabs(next);
    if (!next.length) {
      setActive("");
      closeWorkbench();
      return;
    }
    if (active === id) setActive(next[Math.min(Math.max(idx, 0), next.length - 1)].id);
  };

  const current = tabs.find((t) => t.id === active);
  const className = [
    "workbench",
    open ? "open" : "",
    resizing ? "resizing" : "",
    fullscreen ? "fullscreen" : "",
  ].filter(Boolean).join(" ");

  return (
    <aside
      className={className}
      style={{ ["--wb-width" as string]: `${width}px` }}
      aria-hidden={!open}
    >
      {open && !fullscreen && (
        <div
          className="wb-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整工作台宽度"
          onMouseDown={startResize}
          title="拖拽调整宽度"
        />
      )}
      <div className="workbench-inner">
        <div className="wb-tabs">
          {tabs.length > 0 ? (
            <>
              <div className="wb-tab-scroll">
                {tabs.map((t) => (
                  <div
                    key={t.id}
                    className={`wb-tab ${active === t.id ? "active" : ""}`}
                    onClick={() => setActive(t.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") setActive(t.id);
                    }}
                  >
                    <span className="wb-tab-icon">
                      {t.kind === "files" ? <FolderOpenOutlined /> : <FileTextOutlined />}
                    </span>
                    <span className="wb-tab-title">{t.title}</span>
                    <CloseOutlined
                      className="wb-tab-close"
                      onClick={(e) => {
                        e.stopPropagation();
                        closeTab(t.id);
                      }}
                    />
                  </div>
                ))}
              </div>
              <Tooltip title="打开工作区文件">
                <span className="wb-tab-add" onClick={openFiles}>
                  <PlusOutlined />
                </span>
              </Tooltip>
            </>
          ) : (
            <span className="wb-home-label">开始</span>
          )}
          <Tooltip title={fullscreen ? "退出全屏" : "全屏"}>
            <span className="wb-tab-add" onClick={toggleFullscreen}>
              {fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
            </span>
          </Tooltip>
          <Tooltip title="收起工作台">
            <span className="wb-tab-add" onClick={closeWorkbench}>
              <CloseOutlined />
            </span>
          </Tooltip>
        </div>

        {tabs.length === 0 ? (
          <div className="wb-home">
            <div className="wb-home-sub">打开一个工作区文件，或从下面选择操作</div>
            <div className="wb-home-cards">
              <button type="button" className="wb-card" onClick={openFiles}>
                <span className="wb-card-icon wb-card-icon-file">
                  <FolderOpenOutlined />
                </span>
                <span className="wb-card-text">
                  <span className="wb-card-title">工作区文件</span>
                  <span className="wb-card-desc">浏览当前工作区里的所有文件</span>
                </span>
              </button>
              <div className="wb-card disabled">
                <span className="wb-card-icon">
                  <CodeOutlined />
                </span>
                <span className="wb-card-text">
                  <span className="wb-card-title">新建终端</span>
                  <span className="wb-card-desc">即将支持</span>
                </span>
              </div>
              <div className="wb-card disabled">
                <span className="wb-card-icon">
                  <GlobalOutlined />
                </span>
                <span className="wb-card-text">
                  <span className="wb-card-title">浏览器</span>
                  <span className="wb-card-desc">即将支持</span>
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="wb-content">
            {current?.kind === "files" && (
              <WorkbenchFiles
                workspaceId={workspaceId}
                path={dir}
                onPathChange={setDir}
                onOpenFile={openFile}
              />
            )}
            {current?.kind === "file" && (
              <FilePreview
                key={`${workspaceId}:${current.path}`}
                workspaceId={workspaceId}
                path={current.path}
                name={current.title}
                ext={current.ext}
              />
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
