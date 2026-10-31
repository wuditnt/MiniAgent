# 工具系统

## 用途

所有能力最终都变成同一个 `Tool` 对象、进同一张注册表、出现在同一个 tools 数组里。核心循环因此不需要知道工具从哪来。

## 概念

| 来源 | 位置 | 说明 |
|---|---|---|
| `core` | `src/core/*.py` | 核心工具：`request_approval`、`ask_user`、`remember`、`search_tools`、`delegate`、`use_skill` |
| `builtin` | `builtin_tools/` | 28 个模块、109 个工具，文件 / 代码 / 执行 / 网络 / 视觉 / 系统 / 数据 / 文档 / 表格 / 文档转换 / OCR 等 |
| `user` | `user_tools/` | 你自己写的工具，丢一个 `.py` 就多一个 |
| `mcp` | `mcp/mcp_servers.json` | 外部 MCP server 桥接的工具，名形如 `server__tool` |

**工具档位**（通用配置 `tool_tier`）：

- `standard`（默认）：常用工具常驻，低频能力由模型调 `search_tools` 按需检索加载，每轮省约 1.5 万字符 schema。
- `full`：一次性暴露全部工具。

## 写一个工具

新建 `user_tools/whoami.py`：

```python
from core import tool

@tool
def now() -> str:
    """返回当前时间。"""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
```

Schema 自动生成：名字取函数名，描述取 docstring 首行，参数从类型注解与 `Args:` 段推导。重启后端生效。

进阶：

```python
@tool(exclusive=True)     # 会改共享状态，必须独占执行
def switch_branch(name: str) -> str: ...

@tool(dangerous=True, group="系统")
def kill_process(pid: int) -> str: ...
```

## 办公文档工具组

办公文档按文件类型拆成 5 个互不依赖的模块，一条用户任务只对应一个入口：

| 模块 | 工具 | 路由 |
|---|---|---|
| `word_tools.py` | `read_docx` `write_docx` `docx_to_markdown` | `.docx` |
| `sheet_tools.py` | `read_xlsx` `write_xlsx` `xlsx_merge` `xlsx_split` `xlsx_to_csv` `csv_to_xlsx` `xlsx_format` `xlsx_formula` | `.xlsx/.xlsm/.xls/.xlsb/.ods`、CSV 互转 |
| `pdf_tools.py` | `read_pdf` `read_pdf_tables` `write_pdf` `pdf_merge` `pdf_split` `pdf_rotate` `pdf_extract_images` `images_to_pdf` `pdf_to_images` `pdf_watermark` `pdf_compress` `pdf_encrypt` `pdf_decrypt` | `.pdf`、图片合成 PDF |
| `pptx_tools.py` | `read_pptx` `write_pptx` | `.pptx` |
| `convert_tools.py` | `office_to_pdf` | Office 文档 → `.pdf` |

五个模块只依赖 `core` 的 `clip` / `read_path` / `safe_path` / `tool`，不互相 import。PDF 写类工具默认直接覆盖同名输出，输出路径与源文件相同时先写临时文件再原子替换；`pdf_extract_images` 只提取内嵌位图，扫描页请走 `ocr_pdf`；`office_to_pdf` 依赖本机 LibreOffice（`MINI_AGENT_SOFFICE` 可指定路径），没装时返回安装提示。

## 工具管理页

「系统配置 → 工具管理」：展示全部工具的名称 / 来源 / 分组 / 签名，按 core / builtin / user / mcp 标记，支持查看 `dangerous` / `exclusive` 与分组汇总。

## 加载与故障隔离

- 加载报告把失败记成 `{模块: 错误}`，其它工具照常加载；一个坏工具不会让程序起不来。
- 工具执行异常不中断循环，转成 `[error] ...` 文本还给模型。
- 结果按 `MINI_AGENT_MAX_OUTPUT` 截断（默认 8000 字符），避免一次工具输出撑爆上下文。

## 并行规则

| 阶段 | 线程 | 原因 |
|---|---|---|
| 解析参数、查工具、危险确认 | 主线程串行 | 确认要问人，不能并发弹窗 |
| 工具执行 | 线程池并发 | 子进程 / IO / 网络可重叠 |
| 结果回填 | 主线程按原顺序 | `tool_call_id` 必须一一对应 |

不并行的情况：只有一个调用、关闭并行、本批有 `exclusive=True` 工具。

## 关键代码位置

- 对象与注册表：`src/core/registry.py` 的 `Tool`、`ToolRegistry`、`@tool`、`active_registry()` / `use_registry()`
- Schema：`src/core/schema.py`
- 加载：`src/core/loader.py` 的 `ToolLoader` / `load_tools()`
- 按需检索：`src/core/tool_search.py` 的 `search_tools()` / `register()`
- 执行与并行：`src/core/agent.py` 的 `_prepare()` / `_run_tools()` / `_call_tool()` / `_execute()`
- 接口与页面：`src/core/webui.py` 的 `_handle_tools_list()`；`web/src/components/SettingsModal.tsx`
- 内置工具清单：见 [02-architecture.md](../02-architecture.md#模块地图) 与 `builtin_tools/`
- 测试：`tests/test_tools.py`、`tests/test_tool_search.py`

## 注意事项

- `registry or REGISTRY` 是错误写法：空注册表是 falsy，会静默换成全局注册表。正确写法是 `registry if registry is not None else REGISTRY`。
- `dangerous` 与 `exclusive` 是两件不同的事：前者决定要不要问，后者决定能不能并行。
- 用户工具的改动需要重启后端；技能不需要（技能页操作会自动重载）。
- 工具名大小写不敏感冲突会被加载器拒掉，详见加载报告。
