// Type-aware ESLint config for the augur frontend, run as a whole-program TEST
// (//augur/frontend:eslint_typed_test) rather than via the per-file lint-gate
// aspect. Type-aware rules (no-floating-promises, …) need the full TypeScript
// program built from tsconfig.json, which the granular per-target gate aspect
// can't provide; a test target gets the whole `:main_lib` closure + tsconfig in
// one sandbox (mirrors how tsc_test runs). See plans/eslint_tightening.md (P3).

import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    files: ["**/*.{ts,tsx}"],
    // Inline `eslint-disable` directives in the source target the gate config's
    // rules (e.g. react-hooks/exhaustive-deps); this secondary type-aware pass
    // must not report them as unused.
    linterOptions: { reportUnusedDisableDirectives: "off" },
    languageOptions: {
      parser: tsparser,
      // projectService auto-discovers ./tsconfig.json and builds the program.
      parserOptions: { projectService: true, ecmaVersion: "latest", sourceType: "module" },
    },
    // Register the same plugins the gate config uses so existing inline
    // `eslint-disable` directives (e.g. react-hooks/exhaustive-deps) resolve.
    // Only the type-aware rules below are enabled here.
    plugins: { "@typescript-eslint": tseslint, react, "react-hooks": reactHooks },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      "@typescript-eslint/await-thenable": "error",
    },
  },
];
