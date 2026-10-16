"""
`python -m core` 的入口。

为什么单独一个 __main__.py？
    `python -m 包名` 要求包里有 __main__.py。裸模块名导入的 sys.path 铺设在
    core/__init__.py 里已经完成，所以这里只要把 main.main() 转起来就行。
"""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())