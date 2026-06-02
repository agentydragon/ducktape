// @ts-check
// Workspace-level ESLint configuration for all JS/TS projects.
// Uses flat config cascading: broad configs first, narrow overrides after.

import js from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import sveltePlugin from "eslint-plugin-svelte";
import svelteParser from "svelte-eslint-parser";
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
// x/rspcache/admin_ui is intentionally NOT listed: its BUILD.bazel has no
// js_library targets (the Vite build is a WIP stub), so the lint aspect never
// runs on it. Listing it here would be dead config implying coverage that does
// not exist — re-add once it's Bazelized (per-file js_library + tsc_test).
const reactProjects = ["augur/frontend/**"];
const svelteProjects = ["props/frontend/src/**", "x/agent_server/web/src/**", "airlock/frontend/**"];
const projectGlobs = [...reactProjects, ...svelteProjects];

// All projects' .ts/.tsx get the shared TS rules. .svelte (+ .svelte.ts) come
// only from the Svelte projects; the React projects' .ts/.tsx additionally get
// eslint-plugin-react + react-hooks (the Svelte projects' .ts are plain modules,
// so React rules — notably react-hooks/rules-of-hooks — stay off them).
const tsFiles = projectGlobs.map((g) => `${g}/*.{ts,tsx}`);
const svelteFiles = svelteProjects.map((g) => `${g}/*.svelte`);
const svelteTsFiles = svelteProjects.map((g) => `${g}/*.svelte.ts`);
const reactFiles = reactProjects.map((g) => `${g}/*.{ts,tsx}`);

// Import ordering (TS equivalent of ruff's isort)
// import/order is disabled: eslint-plugin-import-x's import/order rule crashes under
// Bazel's sandboxed execution because resolve() returns null for imports in bazel-out/
// paths and getFilePackagePath calls path.dirname(null). Affects all file types.
const importRules = {
  "import/first": "error",
  "import/order": "off",
  "import/newline-after-import": "error",
  "import/no-duplicates": "error",
};

// typescript-eslint's recommended rule set (ban-ts-comment, no-explicit-any,
// no-empty-object-type, no-unsafe-function-type, prefer-as-const, …), pulled
// from the already-present plugin's eslintrc `recommended` config. Its `.rules`
// also turns off the core rules it supersedes (no-array-constructor,
// no-unused-vars, no-unused-expressions). We layer it under coreRules so our
// explicit overrides (no-unused-vars with the `^_` pattern, etc.) win; `no-undef`
// is already disabled below for the type-name positives TypeScript itself catches.
const tsRecommendedRules = tseslint.configs.recommended.rules;

// Shared quality + TS rules applied everywhere
const coreRules = {
  ...tsRecommendedRules,
  "prefer-const": "error",
  eqeqeq: ["error", "always", { null: "ignore" }],
  "no-console": ["warn", { allow: ["warn", "error"] }],
  "@typescript-eslint/consistent-type-imports": "error",
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
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/build/**",
      "**/.svelte-kit/**",
      "**/playwright-report/**",
      "**/*.config.mjs",
    ],
  },

  js.configs.recommended,

  // ── All TypeScript files ───────────────────────────────────────────────
  {
    files: tsFiles,
    ignores: svelteTsFiles,
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: "latest", sourceType: "module" },
      globals: browserGlobals,
    },
    plugins: tsPlugins,
    rules: coreRules,
  },

  // ── All Svelte files ───────────────────────────────────────────────────
  // Svelte plugin recommended config (scoped to our projects)
  ...sveltePlugin.configs["flat/recommended"].map((config) => ({
    ...config,
    files: svelteFiles,
  })),
  {
    files: svelteFiles,
    languageOptions: {
      parser: svelteParser,
      parserOptions: { parser: tsparser, ecmaVersion: "latest", sourceType: "module" },
      globals: browserGlobals,
    },
    plugins: tsPlugins,
    rules: {
      ...coreRules,
      "svelte/no-unused-svelte-ignore": "error",
    },
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

  // ── study_casino frontend: browser React JS/JSX (no TypeScript) ──────────
  // Not in projectGlobs (deliberately untyped JS), so it only matches js.recommended.
  // Give it browser globals (fetch/window/document/WebSocket/...) and JSX parsing so
  // no-undef doesn't misfire on browser builtins; the two react/jsx-uses-* rules stop
  // no-unused-vars from flagging `React` and components that are only referenced in JSX.
  {
    files: ["x/study_casino/frontend/**/*.{js,jsx}"],
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
];
