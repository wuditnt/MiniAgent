# 文档索引

这里是 mini-agent 的完整技术文档。目标是：**新人接手或 AI 助手进入项目时，不读源码也能先建立完整认知**。

## 怎么读

### 我是第一次接手开发

1. [快速入门](01-quickstart.md) —— 跑起来，知道东西在哪
2. [产品架构](02-architecture.md) —— 分层、核心循环、数据流
3. 按需读 [功能文档](03-features/)：先看 [工具系统](03-features/tools.md)、[沙箱与安全](03-features/sandbox.md)、[会话](03-features/sessions.md)
4. [测试](04-operations/testing.md) 与 [遗留问题](04-operations/known-issues.md)

### 我只想用起来

1. [快速入门](01-quickstart.md)
2. [对话与智能体](03-features/chat-and-agent.md)、[模型与推理等级](03-features/models-and-reasoning.md)
3. 按需看：[联网搜索](03-features/web-search.md)、[附件上传](03-features/attachments.md)、[技能](03-features/skills.md)、[记忆](03-features/memory.md)、[工作台](03-features/workbench.md)、[确认与授权](03-features/approval.md)

### 我是 AI 编程助手

按任务直接跳转，每篇都有「关键代码位置」一节（文件 + 类 / 函数名）：

| 要改什么 | 先读 |
|---|---|
| 核心循环 / 消息裁剪 / 流式 / 并行 | [产品架构](02-architecture.md)、[对话与智能体](03-features/chat-and-agent.md) |
| 加工具 / 改工具加载 | [工具系统](03-features/tools.md)、[沙箱与安全](03-features/sandbox.md) |
| 改前端界面 / 接口 | [产品架构](02-architecture.md)、[工作台](03-features/workbench.md)、[会话](03-features/sessions.md) |
| 改模型接入 / 推理等级 | [模型与推理等级](03-features/models-and-reasoning.md) |
| 改确认、提问、取消 | [确认与授权](03-features/approval.md)、[内联提问](03-features/ask-user.md)、[取消机制](03-features/chat-and-agent.md#取消) |
| 改记忆 / 事件流 / 登录 | [记忆](03-features/memory.md)、[运行轨迹](03-features/trajectory.md)、[登录与账号](03-features/auth-and-access.md) |

## 文档约定

每篇功能文档统一为五节：

1. **用途** —— 它解决什么问题
2. **概念** —— 关键术语与边界
3. **使用方式** —— 界面操作或配置写法
4. **关键代码位置** —— 文件与函数，供接手者与 AI 定位
5. **注意事项** —— 已知限制、坑、与其他模块的耦合

## 目录

```
docs/
├── README.md                     本文
├── 01-quickstart.md              快速入门
├── 02-architecture.md            产品架构
├── 03-features/                  功能特性
│   ├── chat-and-agent.md         对话与智能体双模式、核心循环
│   ├── sessions.md               会话存储、搜索、标题
│   ├── models-and-reasoning.md   模型配置与推理等级
│   ├── web-search.md             联网搜索与抓取
│   ├── approval.md               三档确认与 request_approval
│   ├── ask-user.md               内联提问卡片
│   ├── attachments.md            图片与文件附件
│   ├── skills.md                 技能系统
│   ├── tools.md                  工具系统与工具档位
│   ├── mcp.md                    MCP 连接
│   ├── subagent.md               子智能体（委派）
│   ├── memory.md                 三层记忆系统
│   ├── sandbox.md                沙箱与安全边界
│   ├── workbench.md              工作区文件抽屉（预览）
│   ├── trajectory.md             事件流与运行轨迹
│   ├── auth-and-access.md        登录、账号与权限
│   └── settings.md               通用配置与运行日志
└── 04-operations/
    ├── testing.md                本地测试指南
    ├── troubleshooting.md        常见故障排查
    └── known-issues.md           已知问题与生产化缺口
```

## 相关文档

- [`user_tools/README.md`](../user_tools/README.md) —— 写自定义工具
- [`skills_user/README.md`](../skills_user/README.md) —— 写自定义技能
- [`.trae/documents/`](../.trae/documents/) —— 规划类文档（路线图、桌面化方案），非稳定事实来源
