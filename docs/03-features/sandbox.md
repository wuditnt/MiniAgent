# 沙箱与安全

## 用途

工具会不断扩展，但边界必须只有一处定义。沙箱回答两个问题：**能不能读**、**能不能写**。

## 概念

### 写通道 `safe_path(p)`

用于写 / 删 / 移动 / 编辑 / 解压落地点。目标必须在工作区内，区外一律 `PermissionError`，**没有任何模式可以放开**。

### 读通道 `read_path(p)`

区内放行；区外按 `sandbox_read` 三档判定：

| 档位 | 行为 |
|---|---|
| `strict`（默认） | 越界读写全拦 |
| `whitelist` | 仅白名单绝对路径可读（每行一条） |
| `open` | 沙箱外全部只读放开 |

区外 SQLite 库自动改 `mode=ro` 只读连接，且只允许 SELECT / WITH / PRAGMA / EXPLAIN。

### 三条写旁路，各封一层

1. **`run_python` 注入 prelude 写护栏**：用户代码执行前 patch `open` / `os.remove` / `shutil.*` / `pathlib` / `sqlite3.connect` 等写入口，区外写直接 `PermissionError`；护栏一次性安装后摘除自身，不污染命名空间。
2. **`run_shell` 越界写检测**：重定向（`>` / `>>`）与写动词（copy / move / del / mkdir / …）目标落在区外即拒绝。
3. **zip / tar 解压防穿越（zip-slip）**：逐成员 `realpath` 校验必须在解压目录内；tar 额外拒绝符号链接成员。

### 可取消进程

`run_shell` / `run_python` 底层用 `sandbox.run_process`：Popen + 0.15s 短轮询，取消时杀整棵进程树并返回 `[cancelled]`。不用 `subprocess.run()`，因为它会把线程死死阻塞在内部 wait 上。

## 使用方式

- 「系统配置 → 通用配置 → 沙箱读取范围」切换 `strict` / `whitelist` / `open`。
- 工具作者按语义选通道：

```python
from core import safe_path, read_path

p = safe_path(args["path"])   # 要写
p = read_path(args["path"])   # 只读
```

- 记忆保护区与附件目录由 `memory.is_memory_path()` 统一判定，见 [记忆](memory.md#memory-是保护区)。

## 关键代码位置

- 路径与进程：`src/core/sandbox.py` 的 `safe_path()` / `read_path()` / `assert_can_write()` / `set_read_policy()` / `read_mode()` / `run_process()` / `check_command_write()`
- 输出截断：`clip()` / `set_max_output_chars()`
- 编码与二进制：`read_bytes()` / `looks_binary()` / `decode_bytes()` / `hexdump()`
- 写护栏：`src/core/python_prelude.py`
- 保护目录：`src/core/memory.py` 的 `is_memory_path()`
- 读取档配置：`src/core/settings.py` 的 `sandbox_read` / `sandbox_read_whitelist`
- 解压：`builtin_tools/archive_tools.py`
- 测试：`tests/test_tools.py`、`tests/test_memory.py`、`tests/test_workbench.py`

## 注意事项

- 任何档位下，沙箱外的**写入 / 删除 / 移动**都硬禁，没有放开开关。
- `open` 只降低读门槛，写门槛永远不变。
- 已知边界：shell 与 Python 都是完整语言，变量拼接、编码、别名等混淆写法无法 100% 拦截；可靠底座仍是文件系统层，工具通道 / prelude / 命令检测是纵深防御，不是绝对边界。
- 纯 Python 阻塞调用无法被取消令牌打断（Python 不能安全杀线程），长耗时工具只能等它自己返回。
