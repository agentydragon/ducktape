// @ts-check
// Workspace-level ESLint configuration for all JS/TS projects.
// Uses flat config cascading: broad configs first, narrow overrides after.

import js from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import sveltePlugin from "eslint-plugin-svelte";
import svelteParser from "svelte-eslint-parser";
import importPlugin from "eslint-plugin-import";
import react from "eslint-plugin-react";
import globals from "globals";

// All project source directories (add new TS/Svelte projects here)
const projectGlobs = [
  "props/frontend/src/**",
  "x/agent_server/web/src/**",
  "x/rspcache/admin_ui/src/**",
  "airlock/frontend/**",
];

const tsFiles = projectGlobs.map((g) => `${g}/*.{ts,tsx}`);
const svelteFiles = projectGlobs.map((g) => `${g}/*.svelte`);
const svelteTsFiles = projectGlobs.map((g) => `${g}/*.svelte.ts`);

// Import ordering (TS equivalent of ruff's isort)
const importRules = {
  "import/first": "error",
  "import/order": [
    "error",
    {
      groups: ["builtin", "external", "internal", ["parent", "sibling"], "index", "type"],
      "newlines-between": "always",
      alphabetize: { order: "asc", caseInsensitive: true },
    },
  ],
  "import/newline-after-import": "error",
  "import/no-duplicates": "error",
};

// Shared quality + TS rules applied everywhere
const coreRules = {
  "prefer-const": "error",
  eqeqeq: ["error", "always", { null: "ignore" }],
  "no-console": ["warn", { allow: ["warn", "error"] }],
  "@typescript-eslint/consistent-type-imports": "error",
  "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
  "no-unused-vars": "off",
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
      globals: { ...globals.browser },
    },
    plugins: {
      "@typescript-eslint": tseslint,
      import: importPlugin,
    },
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
      globals: { ...globals.browser },
    },
    plugins: {
      "@typescript-eslint": tseslint,
      import: importPlugin,
    },
    rules: {
      ...coreRules,
      "svelte/no-unused-svelte-ignore": "warn",
    },
  },

  // ── Per-project overrides ──────────────────────────────────────────────

  // RSPCache admin UI: React/JSX
  {
    files: ["x/rspcache/admin_ui/src/**/*.{ts,tsx}"],
    languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { react },
    settings: { react: { version: "18.3" } },
    rules: { "react/react-in-jsx-scope": "off" },
  },
];
