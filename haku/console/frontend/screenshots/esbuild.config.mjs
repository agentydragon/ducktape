// Bundles the screenshot harness into a single IIFE that renders into #app. IIFE format so the
// generated file:// page (render.mjs) can <script>-load it without module CORS restrictions in
// headless Chromium. Output: <outdir>/harness.js.
//
// The entry is `harness.js` — what `:screenshot_harness_lib` (a ts_library) compiles
// `harness.tsx` to — not the `.tsx` source, which no longer reaches this action.
import esbuild from "esbuild";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const outdir = resolve(process.argv[2] || "dist");

await esbuild.build({
  entryPoints: [resolve(__dirname, "harness.js")],
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
