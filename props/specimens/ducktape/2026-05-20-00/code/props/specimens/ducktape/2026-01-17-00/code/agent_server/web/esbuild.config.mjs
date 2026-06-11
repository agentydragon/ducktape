import esbuild from "esbuild";
import esbuildSvelte from "esbuild-svelte";
import tailwindcss from "esbuild-plugin-tailwindcss";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const args = process.argv.slice(2);
const outdir = args[0] || "dist";
const watch = args.includes("--watch");

/** @type {esbuild.BuildOptions} */
const config = {
  entryPoints: [resolve(__dirname, "src/main.ts")],
  bundle: true,
  outdir,
  format: "esm",
  splitting: true,
  minify: !watch,
  sourcemap: true,
  target: ["es2022"],
  plugins: [
    esbuildSvelte({
      compilerOptions: {
        css: "injected",
      },
    }),
    tailwindcss(),
  ],
  // Help esbuild find node_modules in Bazel sandbox
  nodePaths: [resolve(process.cwd(), "node_modules")],
  // Follow symlinks (required for Bazel's node_modules structure)
  preserveSymlinks: false,
  // Support svelte package exports condition
  conditions: ["svelte", "browser", "module", "import"],
  logLevel: "info",
  // Suppress source map warnings from packages with broken source maps
  logOverride: {
    "linked-source-map-not-found": "silent",
    "invalid-source-mappings": "silent",
  },
};

if (watch) {
  const ctx = await esbuild.context(config);
  await ctx.watch();
  console.log("Watching for changes...");
} else {
  await esbuild.build(config);
}
