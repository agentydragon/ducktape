// esbuild option overrides for the production bundle, threaded into the native
// esbuild() rule via spa_bundle's `config` attribute. The rule owns
// entryPoints/bundle/outdir/format/target/sourcemap/minify/splitting and the
// bazel-sandbox module resolver; this file adds the Svelte (CSS injected) +
// Tailwind plugins, the $lib/$components aliases, the .svg=text loader, and the
// Svelte package-export conditions.
import esbuildSvelte from "esbuild-svelte";
import tailwindcss from "esbuild-plugin-tailwindcss";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default {
  plugins: [esbuildSvelte({ compilerOptions: { css: "injected" } }), tailwindcss()],
  alias: {
    $lib: resolve(__dirname, "src/lib"),
    $components: resolve(__dirname, "src/components"),
  },
  loader: { ".svg": "text" },
  conditions: ["svelte", "browser", "module", "import"],
  logOverride: { "invalid-source-mappings": "silent" },
};
