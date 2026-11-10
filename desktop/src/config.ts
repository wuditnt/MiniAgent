/**
 * 桌面端配置：desktop-config.json
 *
 * 只放「桌面壳自己的」设置 —— 模型 Key、工作区这类属于后端的配置继续走
 * /api/settings，不要在这里重复一份。
 */
import * as fs from 'node:fs'
import * as path from 'node:path'
import { app } from 'electron'

/** 点击窗口关闭按钮时的行为：ask 首次询问 / tray 收进托盘 / quit 退出程序 */
export type CloseAction = 'ask' | 'tray' | 'quit'

export interface DesktopConfig {
  /** 是否监听 0.0.0.0（局域网/手机可访问）；保存后需重启生效 */
  lan_enabled: boolean
  /** 关闭按钮行为；ask 表示还没问过用户，首次关闭时弹窗选择并记住 */
  close_action: CloseAction
  /** 上次关闭时的窗口尺寸（仅记忆大小，不记忆位置） */
  window: { width: number; height: number }
}

const DEFAULTS: DesktopConfig = {
  lan_enabled: false,
  close_action: 'ask',
  window: { width: 1280, height: 860 },
}

const CONFIG_FILE = 'desktop-config.json'

/** 可写数据目录：%APPDATA%/mini-agent（与后端 --data-dir 保持一致） */
export function dataDir(): string {
  return path.join(app.getPath('appData'), 'mini-agent')
}

export function configPath(): string {
  return path.join(dataDir(), CONFIG_FILE)
}

export function logsDir(): string {
  return path.join(dataDir(), 'logs')
}

function merge(raw: unknown): DesktopConfig {
  const obj = (raw && typeof raw === 'object' ? raw : {}) as Partial<DesktopConfig>
  const win = (obj.window && typeof obj.window === 'object' ? obj.window : {}) as {
    width?: unknown
    height?: unknown
  }
  return {
    lan_enabled: obj.lan_enabled === true,
    close_action:
      obj.close_action === 'tray' || obj.close_action === 'quit'
        ? obj.close_action
        : DEFAULTS.close_action,
    window: {
      width: Number(win.width) > 400 ? Number(win.width) : DEFAULTS.window.width,
      height: Number(win.height) > 300 ? Number(win.height) : DEFAULTS.window.height,
    },
  }
}

export function readConfig(): DesktopConfig {
  try {
    const text = fs.readFileSync(configPath(), 'utf8')
    // 记事本等编辑器可能写入 BOM，JSON.parse 不接受
    return merge(JSON.parse(text.replace(/^\uFEFF/, '')))
  } catch {
    // 首次启动 / 文件损坏：一律按默认值走，不让配置问题挡住启动
    return merge(null)
  }
}

export function writeConfig(cfg: DesktopConfig): void {
  fs.mkdirSync(dataDir(), { recursive: true })
  fs.writeFileSync(configPath(), JSON.stringify(cfg, null, 2), 'utf8')
}

export function patchConfig(patch: Partial<DesktopConfig>): DesktopConfig {
  const next = { ...readConfig(), ...patch }
  writeConfig(next)
  return next
}

/** 确保数据/日志目录存在（后端自己也会建，但日志转发要先落地） */
export function ensureDirs(): void {
  fs.mkdirSync(logsDir(), { recursive: true })
}
