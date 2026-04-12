import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { port: 5175, strictPort: true },
  build: { outDir: "dist" },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
