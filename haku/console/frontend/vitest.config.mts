import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Specs reach vitest already compiled: each is a `ts_library` whose tsc action type-checked
    // it and emitted the `.js` collected here. Nothing in the runfiles needs transforming, which
    // is also why no `esbuild.jsx` setting is needed — the JSX is long since gone.
    include: ["**/*.test.js"],
    environment: "jsdom", // DOMPurify (in markdown.ts) needs a DOM
  },
  cacheDir: ".vitest-cache",
});
