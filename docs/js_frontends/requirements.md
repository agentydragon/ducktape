# JavaScript Frontend Requirements

## Goals

1. **Bazel builds every registered JS frontend** (see [`pnpm-workspace.yaml`](../../pnpm-workspace.yaml))
2. **Tests work under Bazel**, including visual tests that render PNG screenshots for PR review
3. **Minimal duplication** - single source of truth for package versions
4. **Frontends may share code in future** - don't bake in independence assumption
5. **Linting/formatting via Bazel aspects** - consistent with how we do ruff/mypy for Python

## Non-Goals / Acceptable Tradeoffs

- **Local `pnpm dev` can break** - Bazel is the build system
- **Toolchain flexibility** - can switch bundlers, dev servers, test runners
- **Package manager doesn't matter** - pnpm, npm, bun, whatever works with Bazel
- **Dev server is nice-to-have** - not required

## Current State

- Workspace members: [`pnpm-workspace.yaml`](../../pnpm-workspace.yaml); each member has its own `package.json`
- aspect_rules_js for Bazel integration
- Vite or esbuild via the Bazel rules, depending on the frontend
- A React component harness with Playwright visual render-health tests (props/frontend)

## Known Problems

See [`known_problems.md`](known_problems.md) for the Playwright module-identity investigation and the workspace, lockfile, and `chdir` caveats.
