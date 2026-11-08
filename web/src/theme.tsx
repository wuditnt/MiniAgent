/**
 * 主题状态：跟随系统 / 浅色 / 深色。
 *
 * 为什么存 localStorage 而不是 settings.json？
 *   主题是「这台设备上的显示偏好」—— 手机想深色、电脑想浅色是常见的，
 *   而且不需要登录就能生效、切换没有网络往返。
 *
 * 生效方式：把解析后的结果写到 <html data-theme="dark|light">，
 * 全局 CSS 与 antd 的 algorithm 都从这里取值；系统深浅切换时实时跟随。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ThemeMode = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "ma-theme";
const MEDIA = "(prefers-color-scheme: dark)";

function readMode(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* 隐私模式下 localStorage 可能不可用，退回跟随系统 */
  }
  return "system";
}

function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia(MEDIA).matches;
}

function resolveMode(mode: ThemeMode): ResolvedTheme {
  if (mode === "system") return systemPrefersDark() ? "dark" : "light";
  return mode;
}

/** 把主题写进 DOM：data-theme 供 CSS 使用，theme-color 影响手机状态栏 */
function applyTheme(theme: ResolvedTheme): void {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#141414" : "#ffffff");
}

interface ThemeContextValue {
  /** 用户选择：system / light / dark */
  mode: ThemeMode;
  /** 实际生效：light / dark */
  theme: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readMode);
  const [theme, setTheme] = useState<ResolvedTheme>(() => resolveMode(readMode()));

  // mode 变化 → 立刻解析并写入 DOM（含系统模式的实时跟随）
  useEffect(() => {
    const media = window.matchMedia(MEDIA);
    const update = () => {
      const next = resolveMode(mode);
      setTheme(next);
      applyTheme(next);
    };
    update();
    if (mode !== "system") return;
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [mode]);

  const setMode = useCallback((next: ThemeMode) => {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* 存不了也不影响本次会话 */
    }
    setModeState(next);
  }, []);

  const value = useMemo(() => ({ mode, theme, setMode }), [mode, theme, setMode]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme 必须在 ThemeProvider 内使用");
  return ctx;
}
