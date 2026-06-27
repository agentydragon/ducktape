import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Standard Vite React+TS app. `build.outDir` defaults to `dist/`, which the
// Dockerfile copies into the backend's static dir. The dev server proxies `/api`
// to a locally-running backend so `npm run dev` works against a real haku-state
// clone (point the backend at a clone first).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8080",
    },
  },
});
