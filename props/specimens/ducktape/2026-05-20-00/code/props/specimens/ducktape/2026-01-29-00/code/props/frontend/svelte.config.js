// Minimal svelte config for svelte-check
// Build preprocessing is handled by esbuild-svelte
/** @type {import('svelte/compiler').CompileOptions} */
export default {
  compilerOptions: {
    css: "injected",
  },
};
