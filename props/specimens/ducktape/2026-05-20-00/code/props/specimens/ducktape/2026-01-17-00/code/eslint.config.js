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

export default [
  // Global ignores - must be a standalone config object
  {
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/build/**",
      "**/.svelte-kit/**",
      "**/storybook-static/**",
      "**/playwright-report/**",
      "**/test-results/**",
      "**/.storybook/**",
      "**/generated/**",
      // Build scripts (Node.js tooling, not app code)
      "**/*.config.mjs",
      "**/generate-schema.mjs",
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
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },

  // Agent server web (Svelte+Vite) - with import plugin
  {
    files: ["agent_server/src/agent_server/web/**/*.ts"],
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
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
      "no-unused-vars": "off", // Use @typescript-eslint version instead

      // General code quality
      "no-console": ["warn", { allow: ["warn", "error"] }],
      "no-multiple-empty-lines": ["error", { max: 1 }],
    },
  },

  // Agent server web (Svelte files)
  ...sveltePlugin.configs["flat/recommended"],
  {
    files: ["agent_server/src/agent_server/web/**/*.svelte"],
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
    files: ["rspcache/admin_ui/**/*.{ts,tsx}"],
    ignores: [
      "rspcache/admin_ui/node_modules/**",
      "rspcache/admin_ui/dist/**",
      "rspcache/admin_ui/src/generated/**", // Generated types
    ],
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
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "no-unused-vars": "off",
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
];
