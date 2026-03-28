// @ts-check
// Workspace-level ESLint configuration for all JS/TS projects
// Uses flat config with per-project file patterns

import js from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import sveltePlugin from "eslint-plugin-svelte";
import svelteParser from "svelte-eslint-parser";
import importPlugin from "eslint-plugin-import";
import react from "eslint-plugin-react";
import globals from "globals";

// Shared rules applied to all project config blocks
const sharedRules = {
  "prefer-const": "error",
  eqeqeq: ["error", "always", { null: "ignore" }],
};

// Shared rules for blocks with @typescript-eslint plugin
const tsRules = {
  "@typescript-eslint/consistent-type-imports": "error",
};

export default [
  // Global ignores - must be a standalone config object
  {
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/build/**",
      "**/.svelte-kit/**",
      "**/playwright-report/**",
      // Build scripts (Node.js tooling, not app code)
      "**/*.config.mjs",
    ],
  },

  js.configs.recommended,

  // Props frontend (SvelteKit) - TypeScript files (excluding .svelte.ts)
  {
    files: ["props/frontend/**/*.ts"],
    ignores: ["props/frontend/**/*.svelte.ts"],
    languageOptions: {
      parser: tsparser,
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      "@typescript-eslint": tseslint,
    },
    rules: {
      ...sharedRules,
      ...tsRules,
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },

  // Props frontend (SvelteKit) - Svelte TypeScript files (*.svelte.ts)
  {
    files: ["props/frontend/**/*.svelte.ts"],
    languageOptions: {
      parser: svelteParser,
      parserOptions: { parser: tsparser },
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      svelte: sveltePlugin,
      "@typescript-eslint": tseslint,
    },
    rules: {
      ...sveltePlugin.configs.recommended.rules,
      ...sharedRules,
      ...tsRules,
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },

  // Props frontend (SvelteKit) - Svelte files
  {
    files: ["props/frontend/**/*.svelte"],
    languageOptions: {
      parser: svelteParser,
      parserOptions: { parser: tsparser },
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      svelte: sveltePlugin,
      "@typescript-eslint": tseslint,
    },
    rules: {
      ...sveltePlugin.configs.recommended.rules,
      ...sharedRules,
      ...tsRules,
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },

  // Agent server web (Svelte+Vite) - with import plugin
  {
    files: ["x/agent_server/web/src/**/*.ts"],
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
      },
      globals: {
        ...globals.browser,
      },
    },
    plugins: {
      "@typescript-eslint": tseslint,
      import: importPlugin,
    },
    rules: {
      // Import ordering and placement
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

      // TypeScript
      ...tsRules,
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
      "no-unused-vars": "off", // Use @typescript-eslint version instead

      // General code quality
      ...sharedRules,
      "no-console": ["warn", { allow: ["warn", "error"] }],
      "no-multiple-empty-lines": ["error", { max: 1 }],
    },
  },

  // Agent server web (Svelte files)
  ...sveltePlugin.configs["flat/recommended"].map((config) => ({
    ...config,
    files: ["x/agent_server/web/src/**/*.svelte"],
  })),
  {
    files: ["x/agent_server/web/src/**/*.svelte"],
    languageOptions: {
      parser: svelteParser,
      parserOptions: {
        parser: tsparser,
        ecmaVersion: "latest",
        sourceType: "module",
      },
      globals: {
        ...globals.browser,
      },
    },
    plugins: {
      import: importPlugin,
    },
    rules: {
      // Import ordering and placement
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

      // Svelte-specific
      "svelte/no-unused-svelte-ignore": "warn",

      // General code quality
      ...sharedRules,
      "no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
      "no-console": ["warn", { allow: ["warn", "error"] }],
      "no-multiple-empty-lines": ["error", { max: 1 }],
    },
  },

  // RSPCache admin UI (React)
  {
    files: ["x/rspcache/admin_ui/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: {
          jsx: true,
        },
      },
      globals: {
        ...globals.browser,
      },
    },
    plugins: {
      react,
      "@typescript-eslint": tseslint,
    },
    settings: {
      react: {
        version: "18.3",
      },
    },
    rules: {
      "react/react-in-jsx-scope": "off", // Not needed in React 17+
      ...sharedRules,
      ...tsRules,
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "no-unused-vars": "off",
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
];
