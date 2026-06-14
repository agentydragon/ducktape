// esbuild option overrides for the production bundle, threaded into the native
// esbuild() rule via spa_bundle's `config` attribute. The rule owns
// entryPoints/bundle/outdir/format/target/sourcemap/minify and the bazel-sandbox
// module resolver; this file only adds the Svelte + Tailwind plugins and the
// Svelte package-export conditions.
import esbuildSvelte from "esbuild-svelte";
import tailwindcss from "esbuild-plugin-tailwindcss";

export default {
  plugins: [esbuildSvelte(), tailwindcss()],
  conditions: ["svelte", "browser", "module", "import"],
  logOverride: { "invalid-source-mappings": "silent" },
};
