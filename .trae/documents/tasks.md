# @ 命令实施任务

> 规格：[spec.md](file:///d:/codeworks/mini-agent/.trae/documents/spec.md)
> 每个任务完成后立即验证，不批量攒。

## T1 后端：搜索与 stat 接口

- [x] `src/core/webui.py` 增加 `SEARCH_*` / `STAT_MAX_PATHS` 常量与 `_search_workspace`
- [x] `_handle_workspace_search`（GET `/api/workspace/search`）
- [x] `_handle_workspace_stat`（POST `/api/workspace/stat`）
- [x] 路由注册
- 验证：venv 起服务后 curl 搜索 `q=chat`，确认命中且不含 `node_modules`

## T2 后端：引用解析与注入

- [x] 常量 `MAX_REFERENCES` / `REF_INLINE_BYTES` / `REF_TOTAL_INLINE_BYTES` / `REF_SUMMARY_LINES` / `REF_TEXT_EXTS`
- [x] `_reference_file`、`_guess_mime`、`_resolve_references`
- [x] `_handle_upload` 远程 403
- [x] `_handle_chat` references + 远程 403
- [x] `start_run` 解析 references、写入 `pending_turn`、传入 worker
- [x] `_run_worker` 透传 `_resolve_references`
- 验证：单测覆盖路径安全、去重、上限、模式判定、预算降级

## T3 后端：消息落盘与历史回放

- [x] `_Session` 恢复 `_references`
- [x] `save()` 写 `references`
- [x] `_with_pending_user` 补 `_references`
- [x] `_display_items` 透出 `references`
- 验证：发送带 references 的消息 → `/api/history` 返回 `references`

## T4 agent 支持 references

- [x] `_build_user_message(user_input, attachments, references=None)`
- [x] `run` / `_run_loop` 透传
- [x] `USER_MESSAGE` 事件带引用元数据
- 验证：`tests/test_mention.py` 断言消息文本含 `[引用文件 ...]` 与图片分片

## T5 后端测试

- [x] 新增 `tests/test_mention.py`（搜索、路径校验、注入模式、预算、变更、远程 403、历史回放）
- [x] 在 `tests/run_tests.py` 注册
- 验证：`.\\.venv\\Scripts\\python.exe -m pytest tests -q`

## T6 前端：mention 基础设施

- [x] `types.ts`：`WorkspaceRef`、`WorkspaceSearchItem`、`ChatItem.references`、`HistoryMessage.references`
- [x] `api.ts`：`searchWorkspaceFiles`、`statWorkspaceFiles`
- [x] `mention.ts`：触发、查询词、token 编解码、插入/删除

## T7 前端：输入框与面板

- [x] `MentionMenu.tsx`：分组、防抖搜索、最近使用、上传入口、键盘导航
- [x] `SkillInput.tsx`：双 token 渲染与序列化
- [x] `Chat.tsx`：selectedRefs、面板互斥、多选/拖拽/粘贴上传、发送透传
- [x] `FileRefChips.tsx`：气泡/历史 chip + 变更标注
- [x] `App.tsx`：send 增加 references、乐观气泡、请求体
- [x] `global.css`：样式

## T8 前端构建

- [x] `cd web && npm run build`
- 验证：tsc 无错误、vite 构建成功

## T9 文档与收尾

- [x] `docs/03-features/chat-and-agent.md` 补输入框能力一节
- [x] `docs/README.md` 如需补索引
- [x] 汇报改动清单与验证结果，询问是否提交推送
