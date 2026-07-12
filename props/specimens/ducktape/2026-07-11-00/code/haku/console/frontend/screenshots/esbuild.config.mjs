// Bundles the screenshot harness (harness.tsx) into a single IIFE that renders into
// #app. IIFE format so the generated file:// page (render.mjs) can <script>-load it
// without module CORS restrictions in headless Chromium. Output: <outdir>/harness.js.
import esbuild from "esbuild";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const outdir = resolve(process.argv[2] || "dist");

await esbuild.build({
  entryPoints: [resolve(__dirname, "harness.tsx")],
  bundle: true,
  outdir,
  format: "iife",
  minify: false,
  sourcemap: false,
  target: ["es2022"],
  jsx: "automatic",
  entryNames: "harness",
  nodePaths: [resolve(process.cwd(), "node_modules")],
  preserveSymlinks: false,
  logLevel: "info",
});
