import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    // Skip esbuild's syntax-lowering pass. The PWA targets modern browsers
    // (Chrome 100+/Safari 16+/Firefox 100+/Edge 100+); their native syntax
    // support already covers everything Vite emits, and a recent
    // vite+esbuild combo can fail to lower function-parameter destructuring
    // toward the default target list.
    target: "esnext",
  },
});
