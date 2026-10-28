import process from "node:process";

import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";

import config from "../../../../eslint.config.js";

const project = process.env.BAZEL_ESLINT_TSCONFIG;

export default [
  ...config,
  ...(project
    ? [
        {
          files: ["**/*.{ts,tsx}"],
          languageOptions: {
            parser: tsparser,
            parserOptions: { project: [project], projectService: false, tsconfigRootDir: process.cwd() },
          },
          plugins: { "@typescript-eslint": tseslint },
          rules: {
            "@typescript-eslint/no-floating-promises": "error",
            "@typescript-eslint/no-misused-promises": "error",
            "@typescript-eslint/await-thenable": "error",
          },
        },
      ]
    : []),
];
