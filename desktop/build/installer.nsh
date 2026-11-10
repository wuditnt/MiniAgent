/* 安装/卸载向导的明细面板：打开面板并补充阶段日志。
 *
 * electron-builder 的 NSIS 模板在 common.nsh 中固定写入
 * `ShowInstDetails nevershow`，这里在 customHeader 里覆盖它。
 *
 * 注意：应用负载是整包 7z（app-64.7z）一次性解压，再由 CopyFiles 搬到
 * 安装目录，本身不存在「逐文件拷贝」动作；模板的 installSection.nsh 还会
 * 执行 `SetDetailsPrint none`，所以缺的只是阶段提示，而不是逐文件明细。
 * 下面在 .onInit（解压前）和安装段末尾各补几条 DetailPrint，让用户能看清
 * 当前进度，不至于以为卡住。
 */
!macro customHeader
  ShowInstDetails show
  ShowUninstDetails show
!macroend

/* 解压前：.onInit 阶段执行，先于 installSection 里的 SetDetailsPrint none，
 * 因此这几行会保留在明细面板里，贯穿整个解压过程。 */
!macro customInit
  SetDetailsPrint both
  DetailPrint "正在准备安装 ${PRODUCT_NAME} ..."
  DetailPrint "解压应用文件需要一些时间，期间进度条可能长时间停留在同一位置，属正常现象。"
!macroend

/* 安装段末尾：此时应用文件、注册表、快捷方式都已就绪，补完最后几条。 */
!macro customInstall
  SetDetailsPrint both
  DetailPrint "应用文件已解压到 $INSTDIR"
  DetailPrint "已写入安装信息注册表"
  DetailPrint "已创建开始菜单与桌面快捷方式"
  DetailPrint "安装完成。"
!macroend

/* 卸载段同样补两条，保持面板不空。 */
!macro customUnInstall
  SetDetailsPrint both
  DetailPrint "已移除应用文件与快捷方式"
!macroend
