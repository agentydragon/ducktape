// esbuild option overrides for the production bundle, threaded into the native
// esbuild() rule via spa_bundle's `config` attribute. The rule owns
// entryPoints/bundle/outdir/format/target/sourcemap/minify/splitting and the
// bazel-sandbox module resolver; this file adds the Svelte (CSS injected) +
// Tailwind plugins and the Svelte package-export conditions.
import esbuildSvelte from "esbuild-svelte";
import tailwindcss from "esbuild-plugin-tailwindcss";

export default {
  plugins: [esbuildSvelte({ compilerOptions: { css: "injected" } }), tailwindcss()],
  conditions: ["svelte", "browser", "module", "import"],
  logOverride: {
    "linked-source-map-not-found": "silent",
    "invalid-source-mappings": "silent",
  },
};
