import process from "node:process";

import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";

import config from "../../../../eslint.config.js";

export default [
  ...config,
  {
    files: ["devinfra/lint/x/typed_eslint/fixtures/**/*.{ts,tsx}"],
    // These fixtures sit outside the frontend globs; tsc checks their identifiers.
    rules: { "no-undef": "off" },
  },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        project: ["**/tsconfig_*.json"],
        projectService: false,
        tsconfigRootDir: process.cwd(),
      },
    },
    plugins: { "@typescript-eslint": tseslint },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      "@typescript-eslint/await-thenable": "error",
    },
  },
  {
    files: ["agentplane/app/frontend/connections/**/*.{ts,tsx}"],
    languageOptions: {
      parserOptions: {
        project: ["agentplane/app/frontend/connections/tsconfig.json"],
      },
    },
  },
];
