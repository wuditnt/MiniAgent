/**
 * 预加载脚本：只向页面暴露白名单能力，不开放 Node。
 *
 * 页面前端通过 window.__DESKTOP__ 判断「当前是桌面壳」，
 * 浏览器下该对象不存在，前端继续走原来的纯 Web 逻辑。
 */
import { contextBridge, ipcRenderer } from 'electron'

const api = {
  isDesktop: true,
  platform: process.platform,
  /** 后端连接信息：{ port, version, lanEnabled, dataDir, logFile } */
  backend: () => ipcRenderer.invoke('desktop:backend'),
  /** 桌面端配置：{ lan_enabled, window } */
  getConfig: () => ipcRenderer.invoke('desktop:get-config'),
  /** 保存局域网开关（重启后生效） */
  setLanEnabled: (enabled: boolean) => ipcRenderer.invoke('desktop:set-lan', enabled),
  /** 重启应用（局域网开关生效） */
  restart: () => ipcRenderer.invoke('desktop:restart'),
  /** 打开日志目录（启动失败诊断用） */
  openLogs: () => ipcRenderer.invoke('desktop:open-logs'),
}

contextBridge.exposeInMainWorld('__DESKTOP__', api)
