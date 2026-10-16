# plugins_user —— 你自己的工具放这里

## 三条约定

1. 文件名**不要**以下划线开头（下划线开头的会被加载器忽略）
2. 用 `from core import tool` 装饰你的函数
3. 改完不用重启，控制台里敲 `/reload` 即可生效

> **为什么 `from core import tool` 能直接写？**
> `core` 是 `src/mini_agent/core/` 这个包。加载器在导入插件前会把
> `src/mini_agent/` 放上 `sys.path`（见 `loader.PluginLoader._ensure_on_path`
> 与 `src/mini_agent/_paths.py`），所以插件里不需要写
> `from mini_agent.core import tool` 这种长前缀。
> 两种写法都支持，短的这种是给插件作者留的便利。

## 最小示例：写个普通函数就够了

新建 `plugins_user/my_tools.py`：

```python
from core import tool, safe_path

@tool
def count_words(path: str, top: int = 10) -> str:
    """统计文件里的词频。

    Args:
        path: 相对工作区的文件路径
        top: 返回前 N 个高频词
    """
    words = open(safe_path(path), encoding="utf-8").read().split()
    return f"共 {len(words)} 个词"
```

保存后在控制台敲 `/reload`，然后 `/tools count_words` 就能看到它。

**你没有写一行 JSON** —— 名字取自函数名，描述取自 docstring 首行，
参数类型从类型注解推导，`path` 自动成为必填、`top` 自动带上默认值，
参数说明从 `Args:` 段里逐个抽取。

## 需要更多控制时

```python
@tool(
    "delete_rows",                     # 显式名字
    "删除数据库里满足条件的行。",         # 显式描述（模型靠它决定要不要用）
    params={                            # 覆盖/补充参数描述
        "table": {"type": "string", "description": "表名"},
        "where": {"type": "string", "description": "SQL WHERE 片段"},
    },
    required=["table"],
    group="数据库",                      # 控制台里的分组
    dangerous=True,                     # 标记为危险 → /approve on 时需人工确认
    exclusive=True,                     # 独占：它一出现，本轮所有工具都退回串行
    tags=("db", "write"),
)
def delete_rows(table: str, where: str = "1=1") -> str:
    ...
```

`exclusive=True` 用在**会改动共享状态**的工具上（切 git 分支、改全局配置、
写同一个输出文件）。默认情况下同一轮的多个工具是**并行**执行的，
上面这些操作一旦并行就会互相打架 —— 标记成 exclusive 后，
只要它出现在本批里，整批就退回串行。

## 支持的注解写法

| 写法 | 推导结果 |
|---|---|
| `x: str` | `{"type": "string"}`，且进入 `required` |
| `x: int = 3` | `{"type": "integer", "default": 3}`，不进 `required` |
| `x: float` / `x: bool` / `x: list` / `x: dict` | number / boolean / array / object |
| `x: str \| None = None` | `{"type": "string"}`（None 被剥掉） |
| `x: Optional[int]` | `{"type": "integer"}` |

参数说明支持两种 docstring 风格，也可以用 `params=` 手写覆盖，三者可混用：

```
Args:                     |  :param path: 文件路径
    path: 文件路径          |
```

## 几条实战建议

- **描述决定一切**。模型完全靠 `description` 决定什么时候调你的工具。
  写"处理数据"它就不知道何时用；写"统计 CSV 的行数与列数"它就准确。
- **返回值永远是字符串**。工具之间通过文本和模型对话，不要返回对象。
- **失败用文本表达，不要抛异常**。写 `return "[error] 文件不存在：xxx"`，
  循环会把这段文本交给模型，它自己会换策略；抛异常则会中断当前轮次。
- **碰文件系统必须过 `safe_path()`**。这是工作区沙箱的唯一入口，别绕过它。
- **危险操作用 `dangerous=True` 标注**。这样在 `/approve on` 模式下会先问你。
- **线程安全**：同一轮的多个工具默认**并行**执行，所以你的工具不能依赖
  "上一个工具刚改过的模块级全局变量"。要么用 `exclusive=True` 声明串行，
  要么把状态放在参数里显式传递。返回值、日志、临时文件都要考虑并发。

## 子目录形式的插件

功能多了可以做成包：

```
plugins_user/
  my_tools/
    __init__.py      <- 必须存在
    fs.py
    db.py
```

加载器会自动把它当成 `plugins_user.my_tools` 加载。
