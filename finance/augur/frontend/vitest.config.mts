import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["**/*.test.js"],
  },
  cacheDir: ".vitest-cache",
});
