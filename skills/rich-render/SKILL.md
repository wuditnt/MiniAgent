---
name: UI 渲染
description: Web UI 富渲染输出约定：ECharts 图表代码块、LaTeX 数学公式、GFM 表格与内联 HTML 的正确用法
---

# 富渲染输出约定

你正在一个带富渲染能力的 Web UI 中回答：Markdown、GFM 表格、KaTeX 公式、ECharts 图表、部分内联 HTML 都会被正确渲染。

## 图表：```echarts 代码块

当用户要求「画图 / 可视化 / 柱状图 / 折线图 / 饼图」，或数据对比明显适合图形时，输出 echarts 代码块：

```echarts
{"title":{"text":"月度销量"},"tooltip":{},"xAxis":{"type":"category","data":["1月","2月","3月"]},"yAxis":{"type":"value"},"series":[{"type":"bar","data":[120,200,150]}]}
```

规则：

- 语言标记必须是 `echarts`，内容必须是**合法 JSON**（ECharts option），禁止 JS 对象字面量、函数、注释
- `series.type` ∈ `bar` / `line` / `pie` / `scatter`
- 控制规模：类别 ≤ 20、系列 ≤ 5，保证图清晰可读；必要时配 `legend` 和 `tooltip`
- 图前用一句话说明图的内容，图后给出关键数字结论

## 数学公式

- 行内公式：`$E=mc^2$`
- 独立成行：`$$...$$`
- 需要展示推导/公式时优先用 LaTeX，不要截图式代码块

## 表格

对比类信息优先用 GFM 表格（`| 列 | 列 |`），比纯文本罗列清晰得多。

## 内联 HTML

可用少量内联标签增强表达：`<mark>`、`<sub>`、`<sup>`、`<kbd>`、`<details>/<summary>`。
`<script>`、`<style>`、`iframe`、事件属性会被安全过滤，不要使用。
