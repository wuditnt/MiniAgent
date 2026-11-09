/* 安装/卸载向导打开明细面板，显示安装阶段日志。
 *
 * electron-builder 的 NSIS 模板在 common.nsh 中固定写入
 * `ShowInstDetails nevershow`，这里在 customHeader 里覆盖它。
 * 注意：应用负载是整包 7z 一次性解压，明细里仍不会逐文件列出。
 */
!macro customHeader
  ShowInstDetails show
  ShowUninstDetails show
!macroend
