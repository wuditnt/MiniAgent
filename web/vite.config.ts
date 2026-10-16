import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式后端地址：默认生产端口 8000，可用环境变量覆盖（如指向 8123 的测试服务）
const target = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target, changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        // echarts / katex 体积大且更新频率低，拆成独立 chunk 利于缓存与并行下载
        manualChunks: {
          echarts: ["echarts/core", "echarts/charts", "echarts/components", "echarts/renderers"],
          katex: ["katex"],
        },
      },
    },
  },
});
