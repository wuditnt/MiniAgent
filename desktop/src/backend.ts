/**
 * 后端进程管理：spawn Python 后端 → 解析 READY 握手 → 退出时回收。
 *
 * 约束（见 .trae/documents/desktop-electron-plan.md）：
 *   · 工具必须跑在**真实 Python 解释器**里，所以桌面版内置一份可移植 Python，
 *     这里只负责把它的路径通过 MINI_AGENT_PYTHON / spawn 参数交给后端；
 *   · 端口固定 8317（决策 D4），不换端口、不重试；
 *   · 退出先走 POST /api/shutdown，超时才 taskkill 整棵进程树。
 */
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { createInterface } from 'node:readline'
import { dataDir, ensureDirs, logsDir } from './config'

export const DESKTOP_PORT = 8317

export interface BackendInfo {
  host: string
  port: number
  pid: number
  version: string
  lanEnabled: boolean
  dataDir: string
}

export interface BackendErrorPayload {
  code: string
  hint: string
  port?: number
}

export class BackendStartError extends Error {
  readonly payload: BackendErrorPayload
  readonly logTail: string

  constructor(payload: BackendErrorPayload, logTail: string) {
    super(payload.hint || payload.code)
    this.name = 'BackendStartError'
    this.payload = payload
    this.logTail = logTail
  }
}

interface StartOptions {
  isDev: boolean
  lanEnabled: boolean
}

const READY_TIMEOUT_MS = 45_000
const LOG_TAIL_LINES = 40
const LOG_MAX_BYTES = 2 * 1024 * 1024

function firstExisting(...candidates: string[]): string | null {
  for (const p of candidates) {
    try {
      if (p && fs.existsSync(p)) return p
    } catch {
      /* 忽略：路径不可读就继续找下一个 */
    }
  }
  return null
}

export class Backend {
  private readonly opts: StartOptions
  private child: ChildProcess | null = null
  private info: BackendInfo | null = null
  private stopping = false
  private tail: string[] = []

  /** 后端意外退出（非本壳主动关闭）时回调，供主进程弹诊断框 */
  onUnexpectedExit: ((code: number | null, tail: string) => void) | null = null

  constructor(opts: StartOptions) {
    this.opts = opts
  }

  /** 仓库根 / 安装包资源根 */
  private rootDir(): string {
    return this.opts.isDev
      ? path.resolve(__dirname, '..', '..')
      : path.join(process.resourcesPath, 'app')
  }

  private pythonExe(): string {
    if (this.opts.isDev) {
      const venv = firstExisting(
        path.join(this.rootDir(), '.venv', 'Scripts', 'python.exe'),
        path.join(this.rootDir(), '.venv', 'bin', 'python'),
      )
      return venv || 'python'
    }
    return path.join(process.resourcesPath, 'python', 'python.exe')
  }

  private appendLog(line: string): void {
    this.tail.push(line)
    if (this.tail.length > LOG_TAIL_LINES) this.tail.shift()
    try {
      ensureDirs()
      const file = path.join(logsDir(), 'desktop.log')
      try {
        if (fs.statSync(file).size > LOG_MAX_BYTES) {
          fs.renameSync(file, `${file}.1`)
        }
      } catch {
        /* 文件还不存在 */
      }
      fs.appendFileSync(file, `[${new Date().toISOString()}] ${line}\n`, 'utf8')
    } catch {
      /* 日志写失败不影响主流程 */
    }
  }

  getInfo(): BackendInfo | null {
    return this.info
  }

  /** 启动并等待 READY；失败抛 BackendStartError */
  start(): Promise<BackendInfo> {
    const root = this.rootDir()
    const python = this.pythonExe()
    const script = path.join(root, 'src', 'core', 'main.py')
    this.appendLog(`backend root=${root} python=${python} script=${script}`)
    if (!fs.existsSync(script)) {
      return Promise.reject(
        new BackendStartError(
          { code: 'BACKEND_MISSING', hint: `找不到后端入口：${script}` },
          this.tail.join('\n'),
        ),
      )
    }

    const dist = path.join(root, 'web', 'dist')
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      MINI_AGENT_ROOT: root,
      MINI_AGENT_PYTHON: python,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
      PYTHONDONTWRITEBYTECODE: '1',
    }
    if (fs.existsSync(dist)) env.MINI_AGENT_WEB_DIST = dist

    const args = [
      '-u', script,
      '--desktop',
      '--host', this.opts.lanEnabled ? '0.0.0.0' : '127.0.0.1',
      '--data-dir', dataDir(),
    ]
    this.appendLog(`spawn ${python} ${args.join(' ')}`)

    return new Promise<BackendInfo>((resolve, reject) => {
      let settled = false
      const child = spawn(python, args, {
        cwd: root,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      this.child = child

      const finishError = (payload: BackendErrorPayload) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        reject(new BackendStartError(payload, this.tail.join('\n')))
      }
      const timer = setTimeout(() => {
        finishError({
          code: 'READY_TIMEOUT',
          hint: `后端在 ${READY_TIMEOUT_MS / 1000}s 内没有就绪，请查看日志排查`,
        })
      }, READY_TIMEOUT_MS)

      const onLine = (line: string) => {
        if (!line.trim()) return
        this.appendLog(line)
        if (!line.startsWith('MINI_AGENT_')) return
        const space = line.indexOf(' ')
        const kind = line.slice('MINI_AGENT_'.length, space < 0 ? undefined : space)
        let payload: Record<string, unknown> = {}
        if (space > 0) {
          try {
            payload = JSON.parse(line.slice(space + 1)) as Record<string, unknown>
          } catch {
            return
          }
        }
        if (kind === 'READY') {
          if (settled) return
          settled = true
          clearTimeout(timer)
          const port = Number(payload.port) || DESKTOP_PORT
          this.info = {
            host: String(payload.host || '127.0.0.1'),
            port,
            pid: Number(payload.pid) || child.pid || 0,
            version: String(payload.version || ''),
            lanEnabled: payload.lan_enabled === true,
            dataDir: String(payload.data_dir || dataDir()),
          }
          resolve(this.info)
        } else if (kind === 'ERROR') {
          finishError({
            code: String(payload.code || 'BACKEND_ERROR'),
            hint: String(payload.hint || payload.detail || '后端启动失败'),
            port: Number(payload.port) || undefined,
          })
        }
      }

      if (child.stdout) {
        createInterface({ input: child.stdout }).on('line', onLine)
      }
      if (child.stderr) {
        createInterface({ input: child.stderr }).on('line', (line) => {
          if (line.trim()) this.appendLog(line)
        })
      }

      child.on('error', (err) => {
        finishError({ code: 'SPAWN_FAILED', hint: `无法启动 Python 后端：${err.message}` })
      })

      child.on('exit', (code) => {
        if (!settled) {
          finishError({
            code: 'BACKEND_EXITED',
            hint: `后端启动过程中退出（退出码 ${code ?? '未知'}）`,
          })
        } else if (!this.stopping && this.info) {
          this.onUnexpectedExit?.(code, this.tail.join('\n'))
        }
        this.child = null
      })
    })
  }

  /** 优雅关闭：POST /api/shutdown → 等 3s → 进程树强杀 */
  async stop(): Promise<void> {
    const child = this.child
    if (!child) return
    this.stopping = true
    const port = this.info?.port ?? DESKTOP_PORT
    try {
      await fetch(`http://127.0.0.1:${port}/api/shutdown`, {
        method: 'POST',
        signal: AbortSignal.timeout(1500),
      })
    } catch {
      /* 后端可能已经在退出 */
    }
    const exited = await this.waitExit(3000)
    if (!exited && child.pid) {
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true })
      await this.waitExit(1500)
    }
    this.child = null
  }

  private waitExit(ms: number): Promise<boolean> {
    const child = this.child
    if (!child || child.exitCode !== null) return Promise.resolve(true)
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.removeListener('exit', onExit)
        resolve(false)
      }, ms)
      const onExit = () => {
        clearTimeout(timer)
        resolve(true)
      }
      child.once('exit', onExit)
    })
  }
}

/** 出问题时拼一条可复制的排查命令 */
export function diagnoseCommand(port = DESKTOP_PORT): string {
  return `netstat -ano | findstr :${port}`
}
