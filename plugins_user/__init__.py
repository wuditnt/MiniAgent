"""
plugins_user —— 你自己的插件放这里。

为什么单独开一个目录？
    项目自带的基础插件在 plugins/，升级时会跟着更新；
    你自己的插件在 plugins_user/，不会被覆盖。

约定（只有两条）：
    1. 文件名不要以下划线开头
    2. 用 `from core import tool` 装饰你的函数

改完不用重启，在控制台里敲 /reload 即可生效。
"""
