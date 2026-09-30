import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Specs arrive compiled: each is a ts_library whose tsc action type-checked it and emitted the .js.
    include: ["**/*.test.js"],
  },
  cacheDir: ".vitest-cache",
});
