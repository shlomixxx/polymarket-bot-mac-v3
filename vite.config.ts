import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    port: 5175,
    strictPort: true,
    // Allow access via Cloudflare Tunnel / external hostnames.
    allowedHosts: [".trycloudflare.com", "normal-ratio-fly-processor.trycloudflare.com"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8767",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8767",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist" },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
