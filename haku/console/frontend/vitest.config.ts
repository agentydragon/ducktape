import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["**/*.test.ts"],
    environment: "jsdom", // DOMPurify (in markdown.ts) needs a DOM
  },
  cacheDir: ".vitest-cache",
});
