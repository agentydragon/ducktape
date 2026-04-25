// Production bundle for the Study Casino PWA.
//
// Bundles src/main.jsx → dist/main.js. Copies static assets (index.html,
// sw.js, manifest.webmanifest, icon.svg) verbatim to dist/. The hermetic
// font directory (fonts.css + Outfit/Playfair Display woff2 files) is
// copied to dist/fonts/ so the production app and visual tests both
// render in the same fonts without any network access.
//
// Service worker is NOT bundled because it imports nothing and must be
// served at a stable top-level path (`/sw.js`) so its scope covers the
// whole origin.

import esbuild from "esbuild";
import { copyFile, mkdir } from "fs/promises";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const args = process.argv.slice(2);
const outdir = resolve(args[0] || "dist");
const watch = args.includes("--watch");

await mkdir(outdir, { recursive: true });
await mkdir(resolve(outdir, "fonts"), { recursive: true });

const buildOptions = {
  entryPoints: [resolve(__dirname, "src/main.jsx")],
  bundle: true,
  outdir,
  format: "esm",
  minify: !watch,
  sourcemap: true,
  target: ["es2022"],
  jsx: "automatic",
  loader: { ".js": "jsx" },
  // Resolve node_modules from Bazel's sandboxed layout.
  nodePaths: [resolve(process.cwd(), "node_modules")],
  preserveSymlinks: false,
  logLevel: "info",
};

const STATIC_ASSETS = ["index.html", "sw.js", "manifest.webmanifest", "icon.svg"];
const FONT_ASSETS = ["fonts/fonts.css", "fonts/Outfit-latin.woff2", "fonts/PlayfairDisplay-latin.woff2"];

async function copyStatic() {
  await Promise.all(
    [...STATIC_ASSETS, ...FONT_ASSETS].map((name) =>
      copyFile(resolve(__dirname, name), resolve(outdir, name)),
    ),
  );
}

if (watch) {
  const ctx = await esbuild.context(buildOptions);
  await ctx.watch();
  await copyStatic();
  console.log("watching for changes…");
} else {
  await esbuild.build(buildOptions);
  await copyStatic();
}
