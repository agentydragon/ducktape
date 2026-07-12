import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["**/*.test.ts"],
    environment: "jsdom", // DOMPurify (in markdown.ts) needs a DOM
  },
  // Matches tsconfig.json's "jsx": "react-jsx" — needed explicitly because the Bazel
  // sandbox this runs in doesn't copy tsconfig.json into vitest_test's runfiles (only
  // tsc_test's), so esbuild can't discover it and falls back to the classic transform
  // (which needs `React` in scope — every .tsx file here relies on the automatic one).
  esbuild: { jsx: "automatic" },
  cacheDir: ".vitest-cache",
});
