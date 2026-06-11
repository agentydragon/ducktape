import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// https://vite.dev/config/
export default defineConfig({
  plugins: [svelte()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    // CLI passes --strictPort and actual port; keep default flexible here
    strictPort: false,
    proxy: {
      // Proxy WS to backend; target from env when provided (CLI sets VITE_BACKEND_ORIGIN)
      "/ws": {
        target: (globalThis as any).process?.env?.VITE_BACKEND_ORIGIN || "http://127.0.0.1:8765",
        ws: true,
        changeOrigin: true,
        secure: false,
      },
      // Proxy API calls during dev so window.location.origin works without VITE_BACKEND_ORIGIN
      "/api": {
        target: (globalThis as any).process?.env?.VITE_BACKEND_ORIGIN || "http://127.0.0.1:8765",
        changeOrigin: true,
        secure: false,
      },
      // Example JSON endpoints useful in dev
      "/transcript": {
        target: (globalThis as any).process?.env?.VITE_BACKEND_ORIGIN || "http://127.0.0.1:8765",
        changeOrigin: true,
        secure: false,
      },
    },
  },
  // Emit built assets into the server's static directory for tests/runtime
  build: { outDir: "../server/static", emptyOutDir: true },
});
