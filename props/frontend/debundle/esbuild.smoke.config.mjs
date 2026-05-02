// Smoke-bundle build for the debundle pipeline. Lives next to the production
// `esbuild.config.mjs` but produces a deliberately multi-chunk artifact so
// the pipeline's vendor-swap stage has separate vendor chunks to operate on.
//
// Why a separate config: production ships a single-file bundle (esbuild
// `splitting: true` only kicks in for shared code across multiple entries
// or for dynamic imports, neither of which the prod entry exercises). For
// the smoke we add three entry points alongside `smoke_main.ts`:
//   - `vendor_highlight_marker.ts`: imports highlight.js only.
//   - `vendor_datatable_marker.ts`: imports @careswitch/svelte-data-table only.
//   - `vendor_marker.ts`: imports both, to anchor a third entry.
// Each marker is shared (transitively) only with the main app, so esbuild's
// chunk-graph algorithm puts each vendor's code in its own shared chunk
// rather than collapsing them into one large blob — giving the debundle
// pipeline distinct vendor chunks to swap.
//
// The two vendor packages exercise distinct wrapper shapes:
//   - highlight.js: a CJS package consumed via `import hljs from
//     "highlight.js"` (named-from-default wrapper shape).
//   - @careswitch/svelte-data-table: an ESM package with a named export
//     (`import { DataTable } from "..."`); the wrapper renames the named
//     re-export off the package's module-default boundary.

import esbuild from "esbuild";
import esbuildSvelte from "esbuild-svelte";
import tailwindcss from "esbuild-plugin-tailwindcss";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const projectRoot = resolve(__dirname, "..");

const args = process.argv.slice(2);
const outdir = args[0] || "smoke_bundle";

/** @type {esbuild.BuildOptions} */
const config = {
  entryPoints: {
    main: resolve(__dirname, "smoke_main.ts"),
    vendor_highlight_marker: resolve(__dirname, "vendor_highlight_marker.ts"),
    vendor_datatable_marker: resolve(__dirname, "vendor_datatable_marker.ts"),
  },
  bundle: true,
  outdir,
  format: "esm",
  splitting: true,
  // Keep readable identifiers — the spec selectors below pin against
  // them. Realistic-enough: the bundle still goes through the same
  // emit shape esbuild produces for production (modules, splitting,
  // chunk graph), just without the final identifier-renaming step.
  minify: false,
  sourcemap: false,
  target: ["es2022"],
  plugins: [
    esbuildSvelte({
      compilerOptions: {
        css: "injected",
      },
    }),
    tailwindcss(),
  ],
  alias: {
    $lib: resolve(projectRoot, "src/lib"),
    $components: resolve(projectRoot, "src/components"),
  },
  loader: {
    ".svg": "text",
  },
  nodePaths: [resolve(process.cwd(), "node_modules")],
  preserveSymlinks: false,
  conditions: ["svelte", "browser", "module", "import"],
  logLevel: "info",
  logOverride: {
    "invalid-source-mappings": "silent",
  },
  metafile: true,
};

const result = await esbuild.build(config);
const fs = await import("node:fs");
fs.writeFileSync(resolve(outdir, "metafile.json"), JSON.stringify(result.metafile, null, 2));
