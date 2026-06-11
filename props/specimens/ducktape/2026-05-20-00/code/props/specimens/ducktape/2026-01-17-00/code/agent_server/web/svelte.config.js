/** @type {import("@sveltejs/vite-plugin-svelte").SvelteConfig} */
export default {
  // Consult https://svelte.dev/docs#compile-time-svelte-preprocess
  // for more information about preprocessors
  // Note: vitePreprocess() import causes ESM/CJS issues with svelte-check
  // For type checking, we don't need preprocessing since TypeScript handles it
  compilerOptions: {
    // Enable TypeScript support
    // The actual preprocessing happens during vite build
  },
};
