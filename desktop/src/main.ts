/**
 * Electron 主进程：窗口、单实例、后端生命周期。
 *
 * 加载方式是「后端 URL」而不是 file://：前端所有请求都是相对路径，
 * 同源加载后 SSE / Cookie / 登录态零改造（见方案文档第 2 节）。
 */
import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, shell } from 'electron'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { Backend, BackendStartError, DESKTOP_PORT, diagnoseCommand } from './backend'
import { dataDir, ensureDirs, logsDir, patchConfig, readConfig } from './config'

// 用「安装包里的后端资源是否存在」判断运行形态，而不是 app.isPackaged：
// 开发时直接跑 electron . 也可能被判为已打包，会去 resources 下找不到后端。
const packagesBackend = fs.existsSync(
  path.join(process.resourcesPath, 'app', 'src', 'core', 'main.py'),
)
const isDev = !packagesBackend

let win: BrowserWindow | null = null
let backend: Backend | null = null
let quitting = false

if (process.platform === 'win32') {
  app.setAppUserModelId('com.miniagent.desktop')
}

function createWindow(port: number): void {
  const cfg = readConfig()
  win = new BrowserWindow({
    width: cfg.window.width,
    height: cfg.window.height,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: '#ffffff',
    title: 'mini-agent',
    // 开发态任务栏图标；安装版的图标由 electron-builder 写进 exe
    icon: path.join(app.getAppPath(), 'build', 'icon.ico'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  })

  // 外链一律交给系统浏览器：桌面壳里开新窗口会丢失登录态，也没地址栏
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(`http://127.0.0.1:${port}`)) {
      event.preventDefault()
      if (/^https?:/i.test(url)) void shell.openExternal(url)
    }
  })

  win.on('close', () => {
    if (!win) return
    const [width, height] = win.getSize()
    try {
      patchConfig({ window: { width, height } })
    } catch {
      /* 记忆窗口尺寸失败不算错误 */
    }
  })
  win.on('closed', () => {
    win = null
  })

  win.once('ready-to-show', () => win?.show())
  void win.loadURL(`http://127.0.0.1:${port}/`)
}

function registerIpc(): void {
  ipcMain.handle('desktop:backend', () => {
    const info = backend?.getInfo()
    return {
      port: info?.port ?? DESKTOP_PORT,
      version: info?.version ?? '',
      lanEnabled: info?.lanEnabled ?? false,
      dataDir: info?.dataDir ?? dataDir(),
      logFile: path.join(logsDir(), 'desktop.log'),
    }
  })
  ipcMain.handle('desktop:get-config', () => readConfig())
  ipcMain.handle('desktop:set-lan', (_event, enabled: boolean) => {
    const next = patchConfig({ lan_enabled: enabled === true })
    return { ...next, restart_required: true }
  })
  ipcMain.handle('desktop:restart', async () => {
    quitting = true
    await backend?.stop()
    app.relaunch()
    app.exit(0)
  })
  ipcMain.handle('desktop:open-logs', async () => {
    ensureDirs()
    await shell.openPath(logsDir())
  })
}

async function showStartFailure(err: unknown): Promise<void> {
  const isStartError = err instanceof BackendStartError
  const code = isStartError ? err.payload.code : 'UNKNOWN'
  const hint = isStartError ? err.payload.hint : String(err)
  const tail = isStartError ? err.logTail.split('\n').slice(-12).join('\n') : ''

  const title =
    code === 'PORT_IN_USE'
      ? `启动失败：端口 ${DESKTOP_PORT} 已被占用`
      : '启动失败：mini-agent 后端没有起来'

  const buttons = code === 'PORT_IN_USE'
    ? ['复制排查命令', '打开日志目录', '退出']
    : ['打开日志目录', '退出']

  const { response } = await dialog.showMessageBox({
    type: 'error',
    title: 'mini-agent',
    message: title,
    detail: `${hint}\n\n日志：${path.join(logsDir(), 'desktop.log')}${
      tail ? `\n\n最后几行：\n${tail}` : ''
    }`,
    buttons,
    defaultId: 0,
    cancelId: buttons.length - 1,
    noLink: true,
  })

  const action = buttons[response]
  if (action === '复制排查命令') {
    clipboard.writeText(diagnoseCommand(DESKTOP_PORT))
    await dialog.showMessageBox({
      type: 'info',
      title: 'mini-agent',
      message: '已复制的排查命令',
      detail: `${diagnoseCommand(DESKTOP_PORT)}\n\n在 PowerShell 里粘贴执行，可看到占用端口的进程 PID。`,
      buttons: ['知道了'],
      noLink: true,
    })
    await shell.openPath(logsDir())
  } else if (action === '打开日志目录') {
    await shell.openPath(logsDir())
  }
}

async function bootstrap(): Promise<void> {
  ensureDirs()
  const cfg = readConfig()

  if (!isDev) Menu.setApplicationMenu(null)

  backend = new Backend({ isDev, lanEnabled: cfg.lan_enabled })
  registerIpc()

  backend.onUnexpectedExit = (code, tail) => {
    if (quitting) return
    void dialog
      .showMessageBox({
        type: 'error',
        title: 'mini-agent',
        message: '后端进程意外退出',
        detail: `退出码：${code ?? '未知'}\n\n日志：${path.join(logsDir(), 'desktop.log')}\n\n${tail
          .split('\n')
          .slice(-8)
          .join('\n')}`,
        buttons: ['重启应用', '退出'],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      })
      .then(({ response }) => {
        if (response === 0) {
          app.relaunch()
        }
        void shutdownAndQuit(response === 0 ? 0 : 1)
      })
  }

  try {
    const info = await backend.start()
    createWindow(info.port)
  } catch (err) {
    // 盖住「弹窗之前」的诊断：开发时看终端，打包后看日志文件
    console.error('[desktop] 启动失败', err)
    await showStartFailure(err)
    await shutdownAndQuit(1)
  }
}

async function shutdownAndQuit(code: number): Promise<void> {
  if (quitting) return
  quitting = true
  await backend?.stop()
  app.exit(code)
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!win) return
    if (win.isMinimized()) win.restore()
    win.show()
    win.focus()
  })

  app.whenReady().then(bootstrap).catch(async (err) => {
    dialog.showErrorBox('mini-agent 启动失败', String(err))
    await shutdownAndQuit(1)
  })

  app.on('window-all-closed', () => {
    app.quit()
  })

  app.on('before-quit', (event) => {
    if (quitting) return
    event.preventDefault()
    void shutdownAndQuit(0)
  })
}
