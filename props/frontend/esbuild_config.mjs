// esbuild option overrides for the production bundle, threaded into the native
// esbuild() rule via spa_bundle's `config` attribute. The rule owns
// entryPoints/bundle/outdir/format/target/sourcemap/minify/splitting and the
// bazel-sandbox module resolver; this file adds the automatic React JSX runtime,
// Tailwind plugin, $lib/$components aliases, and the .svg=text loader.
import tailwindcss from "esbuild-plugin-tailwindcss";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default {
  jsx: "automatic",
  plugins: [tailwindcss()],
  alias: {
    $lib: resolve(__dirname, "src/lib"),
    $components: resolve(__dirname, "src/components"),
  },
  loader: { ".svg": "text" },
  nodePaths: [resolve(__dirname, "node_modules")],
  conditions: ["browser", "module", "import"],
};
