"""
user_tools —— 你自己的工具放这里。

为什么单独开一个目录？
    项目自带的基础工具在 builtin_tools/，升级时会跟着更新；
    你自己的工具在 user_tools/，不会被覆盖。

约定（只有两条）：
    1. 文件名不要以下划线开头
    2. 用 `from core import tool` 装饰你的函数

改完重启后端（python -m core）生效，新工具会出现在 Web UI 的「工具管理」里。
"""
