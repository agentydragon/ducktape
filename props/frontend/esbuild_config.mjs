// esbuild options shared by the production bundle and dev.mjs. The native
// esbuild() rule threads this through spa_bundle's `config` attribute and owns
// entryPoints/bundle/outdir/format/target/sourcemap/minify/splitting plus the
// Bazel-sandbox module resolver. The dev server reuses the same JSX runtime,
// Tailwind plugin, aliases, loader, nodePaths, and package conditions.
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
