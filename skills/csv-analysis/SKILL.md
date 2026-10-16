---
name: csv-analysis
description: 分析 CSV 数据并产出可复核的数字结论
---

# CSV 数据分析流程

目标：从 CSV 文件里提取可靠、可复核的统计结论。

## 步骤

1. 先确认编码与结构（不要凭扩展名猜）：
   - 用 `run_shell` 跑 `xxd` / `head` 看魔数与前几行
   - 或用 `run_python` 分别以 utf-8 / gbk / utf-8-sig 尝试解码
2. 再用 `run_python`（csv 模块或 pandas）读取并统计：
   - 总行数、列名、缺失值数量、数值列的均值/极值
3. 结论必须附带证据：给出计数、聚合数字、抽样行号。

## 约束

- 不要整表粘贴原始数据，只给关键片段。
- 遇到编码错误先换编码重试，不要把报错当结论。
- 测试数据通常在 demo_case/inbox 下（如 sales_gbk.csv、sales_utf8bom.csv）。