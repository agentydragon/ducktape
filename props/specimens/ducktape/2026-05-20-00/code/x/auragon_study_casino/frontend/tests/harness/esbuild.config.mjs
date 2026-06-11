// Bundles the visual-test harness into a single IIFE that renders into #root.
// Output goes to tests/harness/dist/harness.js (referenced by the harness's
// index.html). Format must be IIFE so the file:// page can <script>-load it
// without module CORS restrictions in the headless Chromium runner.

import esbuild from "esbuild";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const outdir = resolve(args[0] || "dist");

await esbuild.build({
  entryPoints: [resolve(__dirname, "harness.jsx")],
  bundle: true,
  outdir,
  format: "iife",
  minify: false,
  sourcemap: false,
  target: ["es2022"],
  jsx: "automatic",
  loader: { ".js": "jsx" },
  outExtension: { ".js": ".js" },
  entryNames: "harness",
  nodePaths: [resolve(process.cwd(), "node_modules")],
  preserveSymlinks: false,
  logLevel: "info",
});
