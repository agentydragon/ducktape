# JavaScript Frontend Requirements

## Goals

1. **Bazel builds all 3 JS frontends** (props/frontend, agent_server/web, rspcache/admin_ui)
2. **Tests work under Bazel**, including visual regression tests that generate PNG snapshots
3. **Minimal duplication** - single source of truth for package versions
4. **Frontends may share code in future** - don't bake in independence assumption
5. **Linting/formatting via Bazel aspects** - consistent with how we do ruff/mypy for Python

## Non-Goals / Acceptable Tradeoffs

- **Local `pnpm dev` can break** - Bazel is the build system
- **Toolchain flexibility** - can switch bundlers, dev servers, test runners
- **Package manager doesn't matter** - pnpm, npm, bun, whatever works with Bazel
- **Dev server is nice-to-have** - not required
- **Frameworks stay as-is** (Svelte for 2, React for 1) - rewriting UI code is out of scope

## Current State

- 3 independent JS frontends:
  - **props/frontend**: SvelteKit (file-based routing, SSR disabled, static adapter)
  - **agent_server/web**: Plain Svelte + Vite (main.ts entry, App.svelte root)
  - **rspcache/admin_ui**: React + Mantine (not Svelte)
- pnpm workspace with per-project package.json files
- aspect_rules_js for Bazel integration
- Vite as bundler
- Storybook + Playwright for visual regression tests (props/frontend only)

## Decisions Made

- **SvelteKit replacement OK** if it simplifies Bazel integration
- **Local pnpm dev can break** - Bazel is the source of truth

## Known Problems

1. **Playwright module identity**: pnpm workspaces create separate node_modules trees per project, causing Playwright to load from different paths and crash
2. **Module resolution in sandbox**: Tools expect node_modules relative to their working directory; flattening the workspace breaks this
3. **Version duplication**: Dependencies declared in both root and per-project package.json
