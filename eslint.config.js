// @ts-check
// Workspace-level ESLint configuration for all JS/TS projects.
// Uses flat config cascading: broad configs first, narrow overrides after.

import js from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import vitest from "@vitest/eslint-plugin";
import importPlugin from "eslint-plugin-import-x";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// ── Shared building blocks (factored so the per-glob configs below stay DRY) ──
const browserGlobals = { ...globals.browser };
const tsPlugins = { "@typescript-eslint": tseslint, import: importPlugin };
const reactSettings = { react: { version: "18.3" } };
// Allow intentionally-unused identifiers/args when prefixed with `_`.
const unusedVarsOptions = { argsIgnorePattern: "^_", varsIgnorePattern: "^_" };

// Project source dirs, bucketed by framework so each config block targets the
// right files — and a new project is declared exactly once, in the right list.
//
// Paths are matched against the file's repo-relative path, so a glob that names no real
// directory silently lints nothing — `augur/frontend/**` sat here while augur lived at
// `finance/augur/frontend`, so the React projects were uncovered. When adding one, check it
// fires: append an unused `const` to a file in it and confirm the target fails to build.
//
const reactProjects = [
  "agentplane/app/frontend/**",
  "aiquota/frontend/**",
  "airlock/frontend/**",
  "devinfra/claude/session_export/frontend/**",
  "finance/augur/frontend/**",
  "finance/plaid/spend/ui/**",
  "haku/console/frontend/**",
  "props/frontend/**",
  "util/frontend/**",
];
// Shared TypeScript libraries are not React frontends, but their sources still receive the
// workspace TypeScript rules. Keep these project registrations separate so React rules and
// browser-specific setup remain scoped to the frontend list above.
const nonReactTypeScriptProjects = ["aiquota/shared/**", "haku/shared/bridge_protocol/**"];

// All registered TypeScript projects receive shared TS rules; only React projects receive the
// React and hooks rules below.
const reactTsFiles = reactProjects.map((g) => `${g}/*.{ts,tsx}`);
const nonReactTsFiles = nonReactTypeScriptProjects.map((g) => `${g}/*.ts`);
const reactFiles = reactProjects.map((g) => `${g}/*.{ts,tsx}`);
const testFiles = reactProjects.map((g) => `${g}/*.test.{ts,tsx}`);

// Import ordering (the TS equivalent of ruff's isort) is intentionally OFF.
// import/order crashes under Bazel: ranking an import calls getContextPackagePath,
// which walks up from the source file for a package.json (pkgUp) — but the
// bazel-out execution tree has none above it, so path.dirname(null) throws. Not
// fixable via resolver config or the whole-program test path. A Prettier
// import-sort plugin needs Nix+Bazel+pnpm wiring, so import
// ordering is parked. See devinfra/debug/eslint_import_order_bazel.md for the full investigation.
const importRules = {
  "import/first": "error",
  "import/order": "off",
  "import/newline-after-import": "error",
  // `prefer-inline` is what makes this catch a module imported twice — once for values, once
  // for types. Without it the rule treats `import type {X} from "m"` and `import {y} from "m"`
  // as different categories and leaves both statements in place.
  "import/no-duplicates": ["error", { "prefer-inline": true }],
};

// typescript-eslint's recommended rule set (ban-ts-comment, no-explicit-any,
// no-empty-object-type, no-unsafe-function-type, prefer-as-const, …), pulled
// from the already-present plugin's eslintrc `recommended` config. Its `.rules`
// also turns off the core rules it supersedes (no-array-constructor,
// no-unused-vars, no-unused-expressions). We layer it under coreRules so our
// explicit overrides (no-unused-vars with the `^_` pattern, etc.) win; `no-undef`
// is already disabled below for the type-name positives TypeScript itself catches.
const tsRecommendedRules = tseslint.configs.recommended.rules;
const unregisteredTypeScriptRule = [
  "error",
  {
    selector: "Program",
    message: "Register this TypeScript project in the appropriate project list in eslint.config.js.",
  },
];

// Shared quality + TS rules applied everywhere
const coreRules = {
  ...tsRecommendedRules,
  "prefer-const": "error",
  eqeqeq: ["error", "always", { null: "ignore" }],
  // `inline-type-imports` so the autofix marks a type-only specifier in place rather than
  // splitting a second `import type` statement off the value import it sits next to, which
  // `import/no-duplicates` below would then flag.
  "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
  // recommended sets a plain `error`; override to keep our leading-underscore escape hatch.
  // (recommended already disables the base `no-unused-vars`, so no need to repeat that here.)
  "@typescript-eslint/no-unused-vars": ["error", unusedVarsOptions],
  // TypeScript already resolves identifiers/types; eslint's no-undef misfires on type-only
  // names (e.g. `RequestInit`) and ambient globals, so defer to the compiler.
  "no-undef": "off",
  ...importRules,
};

export default [
  // Global ignores
  {
    ignores: ["**/node_modules/**", "**/dist/**", "**/build/**", "**/playwright-report/**", "**/*.config.mjs"],
  },

  js.configs.recommended,

  // These scripts execute in browser pages or service workers.
  {
    files: ["util/testing/capture-diagnostics.js", "util/testing/capture-diagnostics-fixture.js"],
    languageOptions: {
      parserOptions: { ecmaVersion: "latest", sourceType: "script" },
      globals: browserGlobals,
    },
  },

  // ── TypeScript registration fallback ───────────────────────────────────
  // New projects must opt into a configured TypeScript project list below. Without a fallback,
  // a new path silently receives no TypeScript parser or rules from this flat config.
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: "latest", sourceType: "module", ecmaFeatures: { jsx: true } },
    },
    rules: { "no-restricted-syntax": unregisteredTypeScriptRule },
  },

  // Shared libraries use TypeScript rules without frontend globals or React rules.
  {
    files: nonReactTsFiles,
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: "latest", sourceType: "module" },
    },
    plugins: tsPlugins,
    rules: { ...coreRules, "no-restricted-syntax": "off" },
  },

  // ── Registered React TypeScript projects ───────────────────────────────
  {
    files: reactTsFiles,
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: "latest", sourceType: "module" },
      globals: browserGlobals,
    },
    plugins: tsPlugins,
    rules: { ...coreRules, "no-restricted-syntax": "off" },
  },

  // ── React projects (eslint-plugin-react recommended + react-hooks) ──────
  {
    files: reactFiles,
    languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { react, "react-hooks": reactHooks },
    settings: reactSettings,
    rules: {
      ...react.configs.recommended.rules,
      "react/react-in-jsx-scope": "off", // automatic JSX runtime, no React import needed
      "react/prop-types": "off", // TypeScript handles prop types
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
    },
  },

  // ── Tests: vitest's recommended rules, every one an error ──
  // A focused or skipped test changes which tests run without failing anything; an `expect` that is
  // never awaited, or a test without one, passes without checking.
  {
    files: testFiles,
    plugins: { vitest },
    rules: {
      ...Object.fromEntries(Object.keys(vitest.configs.recommended.rules).map((rule) => [rule, "error"])),
      // vitest's `expect(value, message)` takes the failure message second; the rule defaults to Jest's one argument.
      "vitest/valid-expect": ["error", { maxArgs: 2 }],
    },
  },

  // ── study_casino frontend: browser React JS/JSX (no TypeScript) ──────────
  // Not in projectGlobs (deliberately untyped JS), so it only matches js.recommended.
  // Give it browser globals (fetch/window/document/WebSocket/...) and JSX parsing so
  // no-undef doesn't misfire on browser builtins; the two react/jsx-uses-* rules stop
  // no-unused-vars from flagging `React` and components that are only referenced in JSX.
  {
    files: ["study_casino/frontend/**/*.{js,jsx}"],
    languageOptions: {
      parserOptions: { ecmaVersion: "latest", sourceType: "module", ecmaFeatures: { jsx: true } },
      globals: browserGlobals,
    },
    plugins: { react },
    settings: reactSettings,
    rules: {
      "react/jsx-uses-react": "error",
      "react/jsx-uses-vars": "error",
      // Match the repo's policy elsewhere (coreRules): unused vars are build-blocking
      // errors, with a leading-underscore escape hatch.
      "no-unused-vars": ["error", unusedVarsOptions],
    },
  },

  // ── Node scripts (.mjs): bundler configs, scenario emitters, worker checks ──
  // These run under Node, not the browser, so grant Node globals (process/console/setTimeout/…).
  // Without this they trip no-undef under js.recommended's env-less default.
  {
    files: ["**/*.mjs"],
    languageOptions: { globals: globals.node },
  },
];
