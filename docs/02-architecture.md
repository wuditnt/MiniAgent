# 产品架构

## 用途

一篇讲清楚 mini-agent 由哪些层组成、一次请求如何从浏览器走到模型再回到界面、以及扩展点在哪。

## 一句话定位

一个**可扩展的本地智能体 Web 应用**：核心循环 + 工具注册表 + 工具目录 + 连接（MCP）双通道。核心循环约 250 行，加工具不需要改核心代码。

## 分层

```
浏览器 (React + Ant Design X)
        │  HTTP / SSE
        ▼
Web UI 服务层   src/core/webui.py      ← 会话、工作区、模型、技能、设置、工作台、认证
        │
        ▼
Agent 核心循环  src/core/agent.py      ← 消息 → LLM(流式) → tool_calls → 并行执行 → 按序回填
        │
        ├── 工具注册表  src/core/registry.py
        │      ├── core     核心工具（approval / ask / memory / tool_search / subagent / use_skill）
        │      ├── builtin  builtin_tools/（28 个模块）
        │      ├── user     user_tools/
        │      └── mcp      MCP server 桥接出的工具
        │
        ├── 能力层  sandbox / approval / ask / cancel / memory / events / images
        │
        └── 模型接入  model_config.py → OpenAI 兼容接口
```

**依赖方向是单向的**：Web UI 依赖 Agent，Agent 依赖注册表与能力模块；能力模块不反向依赖 Web UI。`agent.py` 不判断工具来源（没有 `isinstance` 或来源分支），所以新增通道不需要改循环。

## 一次请求的完整链路

```text
1. 浏览器 POST /api/chat（或 SSE 请求）带 session id、消息、模式、模型、确认档
2. webui.py 校验登录 / 会话锁（同一会话同时只跑一个 run，否则 409）
3. _Session 复用或创建 Agent（工具注册表、技能加载器、MCP 客户端都在这里装配）
4. Agent.run()
   a. 重建 system prompt：基础提示 + 记忆块 + 技能清单 +（auto 档）责任准则
   b. 上下文裁剪：裁旧工具结果 → 滚动摘要 → 滑动窗口
   c. 请求模型（流式），把分片拼回完整消息
   d. 模型返回 tool_calls → _run_tools()
        - 主线程串行：解析参数、查工具、危险确认（不能并发弹窗）
        - 线程池并发：真正的工具执行
        - 主线程按原顺序回填结果（tool_call_id 必须一一对应）
   e. 把工具结果作为 tool 消息塞回，进入下一轮；直到模型不再调用工具
   f. 达到最大轮次时，发一次不带工具的收尾调用
5. 期间所有 UI 事件（增量文本、工具开始/结束、确认、提问、取消）经 SSE 推给前端
6. 结束后写会话 JSON；同时 EventWriter 追加事件流（可追溯运行）
```

## 核心循环关心什么

`agent.py` 里真正重要的只有几件事：

- 流式聚合：`content` 累加；`tool_calls` 按 `index` 合并（`id`/`name` 只在第一片出现，`arguments` 逐片累加）；`usage` 藏在 choices 为空的统计帧里。
- 并行与顺序：并行执行、按原顺序回填；`exclusive=True` 的工具独占本批。
- 上下文控制：`_trim_context()` 三级收敛（裁工具结果 → 滚动摘要 → 硬截断），以「轮」为单位，`tool_calls` 与 `tool` 结果成对保留或删除。
- 取消：每次 `run()` 新建 `CancellationToken`，所有阻塞点轮询；取消时杀进程树、关客户端、补齐缺失的 tool 回复、通知 MCP 对端。
- 异常兜底：工具异常转成 `[error] ...` 文本还给模型，不中断循环。
- 危险确认：永远在主线程串行做。
- 轮次上限与收尾。

## 三条扩展路径

| 路径 | 怎么做 | 出现形式 |
|---|---|---|
| 工具文件 | 往 `user_tools/` 丢一个 `.py` | 进程内函数，进同一张注册表 |
| 连接（MCP server） | 改 `mcp/mcp_servers.json` | 独立进程，工具名 `server__tool` |
| 内置工具 | 改 `builtin_tools/*.py` | 随项目发布 |

外加**技能（Skill）**：模型可遵循的工作流（`SKILL.md`），启动时只注入一行清单，命中后通过 `use_skill` 拉正文进上下文。

## 数据与持久化

| 数据 | 位置 | 说明 |
|---|---|---|
| 会话 | `~/.mini-agent/sessions/<sid>.json` | 恢复历史、搜索、展示的主口径，原子写 |
| 事件流 | `~/.mini-agent/sessions/<sid>.events.jsonl` | append-only 事实层，可完整重建请求 |
| 用户画像 | `~/.mini-agent/memory/user_profile.md` | 跨工作区 |
| 项目记忆 / 主题摘要 | `<工作区>/.memory/` | 随项目目录迁移 |
| 配置 | `~/.mini-agent/settings.json`、`models.json`、`workspaces.json` | 可写数据不进仓库 |
| 账号 | `~/.mini-agent/auth.db` | SQLite |
| 日志 | `~/.mini-agent/logs/<日期>/` | app / llm / tool / error 四类 |

## 模块地图

| 模块 | 职责 |
|---|---|
| `agent.py` | 核心循环、流式、并行、裁剪、记忆注入、收尾、取消收场 |
| `registry.py` | `Tool` / `ToolRegistry` / `@tool` / 当前注册表上下文 |
| `schema.py` | 从类型注解 + docstring 推导 JSON Schema |
| `loader.py` | 工具发现、加载、热重载、故障隔离 |
| `skills.py` | 技能发现、清单注入、`use_skill`、启停状态 |
| `mcp.py` | MCP stdio 客户端（并发安全、可取消、取消通知） |
| `sandbox.py` | 工作区沙箱、双通道路径、三档读取、shell 越界写检测、可取消进程 |
| `python_prelude.py` | `run_python` 写护栏 |
| `approval.py` | 三档确认 + `request_approval` |
| `ask.py` | 内联提问 + `ask_user` |
| `cancel.py` | 取消令牌、杀进程树 |
| `memory.py` | 三层记忆、保护区判定、注入渲染、`remember` |
| `events.py` | 事件流写入与重放 |
| `subagent.py` | 只读子代理与 `delegate` |
| `tool_search.py` | `search_tools` 按需检索工具 |
| `images.py` | 图片附件通道 |
| `webui.py` | HTTP + SSE 服务、会话/工作区/模型/技能/设置接口 |
| `workspace_config.py` | 工作区注册表与沙箱根解析 |
| `model_config.py` | 多服务商模型配置、推理等级、连接测试 |
| `settings.py` | 通用配置读写与应用 |
| `logging_setup.py` | 文件日志 |
| `auth.py` | 账号库、密码哈希、会话、限流 |
| `net_mode.py` | 本轮联网开关（按会话） |
| `mhtml.py` | MHTML 解析 |
| `_paths.py` | 项目根锚点 `ROOT` |

前端主要文件：

| 文件 | 职责 |
|---|---|
| `web/src/App.tsx` | 应用壳、状态编排 |
| `web/src/components/Chat.tsx` | 聊天主界面 |
| `web/src/components/Sidebar.tsx` | 会话 / 工作区导航 |
| `web/src/components/ToolChain.tsx` | 工具执行轨迹 |
| `web/src/components/Trajectory.tsx` | 运行轨迹抽屉 |
| `web/src/components/Workbench.tsx` / `WorkbenchFiles.tsx` | 右侧工作台 |
| `web/src/components/preview/registry.tsx` | 预览格式注册表 |
| `web/src/components/composer/slashCommands.ts` | 斜杠快捷指令注册表 |

## SSE 事件

`webui.py` 用标准库 `ThreadingHTTPServer` + SSE，Python 侧零新依赖。前端收到的事件大致分三类：

- 消息类：增量文本、轮次开始/结束、最终回答
- 过程类：工具开始/结束、确认请求、提问请求、记忆写入提示
- 事实类：`trace` 帧（运行轨迹实时镜像），结束后前端再拉全量兜底

任务执行在服务端 worker 线程：关闭页面只停止推送，不中断执行；重新打开可看到完整结果。同一会话后台运行时发新消息返回 409。

## 关键代码位置

- 循环：`agent.py` 的 `Agent.run()` → `_run_loop()` → `_request()` / `_consume_stream()` / `_run_tools()`
- 裁剪：`_trim_context()` / `_trim_old_tool_results()` / `_roll_summary()` / `_hard_window()`
- 收尾：`_wrap_up_on_max_turns()` / `_max_turns_fallback()`
- 取消：`cancel.py` 的 `CancellationToken`；`agent.py` 的 `_patch_cancelled_replies()`
- 注册表：`registry.py` 的 `ToolRegistry`、`@tool`；`schema.py` 的推导函数
- 加载：`loader.py` 的 `ToolLoader` / `load_tools()`
- Web 服务：`webui.py` 的 `WebServer`、`_RequestHandler.do_GET/do_POST`、`_handle_chat_stream()`
- 前端壳：`web/src/App.tsx`、`web/src/components/Chat.tsx`

## 注意事项

- 核心循环与具体模型 / 服务商解耦：`client=` 可注入任意客户端，所以循环测试不需要真实 API。
- 工具与技能是两种东西：工具是模型能**调用**的能力，技能是模型能**遵循**的工作流。
- 事件流是事实层，会话 JSON 是主口径，两者双写；删除会话时一起删。
- 大部分行为开关都能在「系统配置 → 通用配置」里改，取值优先级：命令行显式参数 > 配置 > 环境变量 > 内置默认。
- 更细的机制见各功能文档；已知缺口见 [04-operations/known-issues.md](04-operations/known-issues.md)。
