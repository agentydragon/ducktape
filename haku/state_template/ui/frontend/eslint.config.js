// ESLint flat config for the Vite React+TS SPA. `pnpm run lint` (eslint .) is a CI gate
// (see ../.forgejo/workflows/lint.yaml, run standalone rather than under Bazel): it fails
// the job on any error. Lints src/**/*.{ts,tsx}, test files included.
//
// Rule scope = the standard Vite React-TS set: ESLint + typescript-eslint recommended,
// the two classic react-hooks rules, and react-refresh's fast-refresh boundary check.
//
// react-hooks is enabled as two explicit rules rather than its packaged `recommended`
// config: eslint-plugin-react-hooks v7 (the only line that satisfies ESLint 10's peer
// range) turns its experimental React-Compiler rules (set-state-in-effect, purity, …) on
// by default. Those flag valid existing patterns and would require behavioral refactors,
// which are out of scope for a lint gate. rules-of-hooks + exhaustive-deps are the
// well-established correctness rules everyone means by "the react-hooks lint".
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  // src/api is generated (openapi-typescript → schema.d.ts); Bazel materializes it
  // into the sandbox for eslint_test, so ignore it — generated types aren't linted.
  { ignores: ["dist", "node_modules", "src/api"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended, reactRefresh.configs.vite],
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  }
);
