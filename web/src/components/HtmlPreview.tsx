/**
 * HTML / MHTML 预览：把内容装进 sandbox iframe 渲染。
 *
 * 关键点：
 *  · sandbox="allow-scripts"（不带 allow-same-origin）→ 脚本可运行，但拿不到父页面
 *    DOM / localStorage / cookie；
 *  · 注入 <base> 指向工作区只读资源接口，HTML 里的相对 CSS/图片/JS 能正常加载；
 *  · MHTML 的内嵌资源以 data: URL 形式传入，按 cid / Content-Location 别名替换。
 */
import { useMemo, type ReactNode } from "react";

interface Props {
  html: string;
  workspaceId: string;
  filePath: string;
  /** MHTML：资源键 → data URL */
  resources?: Record<string, string>;
}

function dirOf(path: string): string {
  const parts = path.split("/");
  parts.pop();
  return parts.filter(Boolean).map(encodeURIComponent).join("/");
}

function assetBase(workspaceId: string, filePath: string): string {
  const dir = dirOf(filePath);
  return `/api/workspace/asset/${encodeURIComponent(workspaceId)}/${dir ? dir + "/" : ""}`;
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function lookupResource(
  raw: string,
  resources: Record<string, string>
): string | undefined {
  const value = raw.trim();
  if (!value) return undefined;
  const candidates = [
    value,
    safeDecode(value),
    value.replace(/^\.\//, ""),
    safeDecode(value).replace(/^\.\//, ""),
  ];
  for (const key of candidates) {
    if (resources[key]) return resources[key];
  }
  return undefined;
}

function buildSrcDoc(
  html: string,
  workspaceId: string,
  filePath: string,
  resources?: Record<string, string>
): string {
  const doc = new DOMParser().parseFromString(html || "", "text/html");
  const head = doc.head || doc.createElement("head");

  // 确保按 UTF-8 渲染
  if (!head.querySelector("meta[charset]")) {
    const meta = doc.createElement("meta");
    meta.setAttribute("charset", "utf-8");
    head.insertBefore(meta, head.firstChild);
  }

  // 相对资源基准：/api/workspace/asset/<wid>/<文件目录>/
  let base = head.querySelector("base");
  if (!base) {
    base = doc.createElement("base");
    head.insertBefore(base, head.firstChild);
  }
  base.setAttribute("href", assetBase(workspaceId, filePath));

  // MHTML：cid / Content-Location → data URL
  if (resources && Object.keys(resources).length > 0) {
    doc.querySelectorAll("[src], [href], [poster]").forEach((el) => {
      for (const attr of ["src", "href", "poster"]) {
        const value = el.getAttribute(attr);
        if (!value) continue;
        const hit = lookupResource(value, resources);
        if (hit) el.setAttribute(attr, hit);
      }
    });

    // <style> 里的 url(cid:xxx) 也替换掉
    const keys = Object.keys(resources).sort((a, b) => b.length - a.length);
    doc.querySelectorAll("style").forEach((style) => {
      let text = style.textContent || "";
      for (const key of keys) {
        if (!key || key.length < 3 || !text.includes(key)) continue;
        text = text.split(key).join(resources[key]);
      }
      style.textContent = text;
    });
  }

  // 根相对路径（/images/a.png）改写到工作区资源接口
  const root = assetBase(workspaceId, "");
  doc.querySelectorAll("[src], [href], [poster]").forEach((el) => {
    for (const attr of ["src", "href", "poster"]) {
      const value = el.getAttribute(attr);
      if (!value || value.startsWith("//") || !value.startsWith("/")) continue;
      if (value.startsWith("/api/workspace/")) continue;
      el.setAttribute(attr, root + value.replace(/^\/+/, ""));
    }
  });

  return "<!doctype html>\n" + doc.documentElement.outerHTML;
}

export default function HtmlPreview({ html, workspaceId, filePath, resources }: Props): ReactNode {
  const srcDoc = useMemo(
    () => buildSrcDoc(html, workspaceId, filePath, resources),
    [html, workspaceId, filePath, resources]
  );
  return (
    <div className="wb-html">
      <iframe
        className="wb-html-frame"
        title={`预览：${filePath}`}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        srcDoc={srcDoc}
      />
    </div>
  );
}
