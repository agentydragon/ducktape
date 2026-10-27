import process from "node:process";

import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  { ignores: ["**/*.js", "**/*.jsx"] },
  {
    files: ["**/*.{ts,tsx}"],
    // Existing inline disables apply to the regular ESLint aspect. This second,
    // type-aware pass should not report them as unused.
    linterOptions: { reportUnusedDisableDirectives: "off" },
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: process.cwd(),
        ecmaVersion: "latest",
        sourceType: "module",
      },
    },
    plugins: { "@typescript-eslint": tseslint, react, "react-hooks": reactHooks },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
    },
  },
];
