/**
 * ECharts 图表封装：按需注册柱/线/饼/散点 + 常用组件（模块化引入，控制包体积）。
 * 用法：<Chart option={{...}} />，option 为 ECharts 的标准 option JSON。
 */
import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "../theme";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  ToolboxComponent,
  CanvasRenderer,
]);

export default function Chart({ option }: { option: Record<string, unknown> }) {
  const ref = useRef<HTMLDivElement>(null);
  const { theme } = useTheme();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // 深色下用 echarts 内置 dark 主题，坐标轴/图例文字自动变浅
    const chart = echarts.init(el, theme === "dark" ? "dark" : undefined, { renderer: "canvas" });
    chart.setOption(option);
    // 容器尺寸变化（窗口缩放、气泡宽度变化）时自适应
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
    };
  }, [option, theme]);

  return <div ref={ref} className="echarts-box" />;
}
