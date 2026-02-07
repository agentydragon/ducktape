# Bazel JavaScript/TypeScript Rules Exploration

## Overview

This document comprehensively surveys all available Bazel rulesets for JavaScript/TypeScript development, with focus on dependency management, module resolution, workspace support, and linting integration. The goal is to understand all available options for building 3 JS frontends (2 Svelte, 1 React) with Bazel.

---

## 1. Recommended Primary Ruleset

### aspect_rules_js (formerly rules_js from Aspect Build)

**Repository:** <https://github.com/aspect-build/rules_js>

**Current Status:** Actively maintained, recommended for all new projects

#### Overview

- High-performance Bazel integration for JavaScript/Node.js based on **pnpm**
- Lazy dependency resolution: only fetches/installs npm packages needed for requested build/test targets
- Works seamlessly with Node.js module resolution (no pathMapping hacks needed)
- Written by the original authors of build_bazel_rules_nodejs after learning from 4 years of experience

#### Dependency Management

**How it works:**

- Uses **pnpm-lock.yaml** as the single source of truth
- Mirrors pnpm's lock file into Starlark code
- Uses Bazel repository rules to fetch individual packages
- Only downloads packages actually needed for the requested targets
- Each package's integrity hash is verified (supply-chain security)

**Package Manager Support:**

- Primary: **pnpm** (recommended and best supported)
- Limited npm support via `npm_translate_lock`
- Limited yarn support via `yarn_lock` attribute (but **does NOT support** pnpm-workspace.yaml)

**Module Resolution:**

- Uses pnpm's symlink-based node_modules structure, which aligns with Bazel's external repositories model
- No TypeScript rootDirs pathMapping hacks needed
- Runs JS tools with working directory in Bazel's output tree under bazel-out
- Creates node_modules tree via pnpm-style layout, all resolutions work naturally

#### Workspace Support

**Full support for pnpm workspaces:**

- Declared via `pnpm-workspace.yaml`
- Each workspace member has its own `package.json`
- Single lockfile across all members (`pnpm-lock.yaml`)
- aspect_rules_js reads workspace configuration and automatically sets up dependencies

**Configuration in MODULE.bazel:**

```starlark
npm = use_extension("@aspect_rules_js//npm:extensions.bzl", "npm", dev_dependency = True)
npm.npm_translate_lock(
    name = "npm_ducktape",
    npmrc = "@@//:.npmrc",
    pnpm_lock = "//:pnpm-lock.yaml",
    data = [
        "//:package.json",
        "//:pnpm-workspace.yaml",
        "//:workspace/member1/package.json",
        "//:workspace/member2/package.json",
    ],
    update_pnpm_lock = True,  # Auto-regenerate on package.json changes
    public_hoist_packages = { ... },  # For runtime discovery of plugins
)
```

#### Known Limitations & Issues

1. **Module Identity Mismatch (Critical - Affects Playwright/Multi-Instance Packages)**
   - When a package like `@playwright/test` re-exports from `playwright`, symlink resolution can cause the same module to be loaded with different filesystem paths
   - Node.js caches modules by resolved path, not by identity
   - This breaks packages that use module identity for internal state (e.g., Playwright's test registry)
   - Documented in ducktape investigation: `props/frontend/docs/playwright-bazel-investigation.md`
   - **Workaround:** Use native Playwright install or rules_playwright directly instead of aspect_rules_js wrappers

2. **Sandbox Symlink Escape via esbuild (Issue #58 aspect_rules_esbuild)**
   - esbuild leaves the sandbox by following symlinks when `preserveSymlinks` is off
   - FSs monkey patches don't cover all ESM entry points (.mjs files)
   - Node.js 19.8+ added new fs APIs not covered by rules_js patches (potential sandbox escape)
   - **Mitigation:** Ensure inputs include all transitive npm dependencies; avoid relying on workspace pnpm install

3. **ESM Import Sandbox Escape (Issue #362)**
   - `realPathSync` in Node.js's ESM resolver is not patched while CJS require loader is
   - Affects .mjs entry points
   - Requires `--preserve-symlinks-main` flag to prevent sandbox escape
   - **Mitigation:** Use `--preserve-symlinks-main=true` in js_binary/js_library

4. **Framework Integration Challenges**
   - **SvelteKit:** Vite is built to watch source repo changes, but Bazel builds in sandbox. Needs special configuration.
   - **Next.js:** "read-only file system" errors when building in sandbox (historically problematic)
   - **React:** Works well with webpack-cli or esbuild; no inherent issues

5. **Development Server Setup**
   - `js_run_devserver` doesn't watch source files directly (Bazel sandbox limitation)
   - Requires explicit file dependencies in `data` attribute
   - Works best with Vite (which supports explicit deps)

#### Aspect-Based Linting Support

**Via aspect_rules_lint (separate ruleset):**

- ESLint: Full support, configurable via `.eslintrc` files
- Prettier: Full support, configurable via `.prettierrc` files
- No changes needed to BUILD files — lint applied as aspect on existing \*\_library targets
- Run with: `bazel lint //src:all` (requires Aspect CLI)
- Can apply fixes with `--fix` flag
- Integrates with Remote Execution and Remote Cache

**Hoist configuration:**

- ESLint plugins must be hoisted for runtime discovery
- Configure in MODULE.bazel `npm_translate_lock` via `public_hoist_packages`:
  ```starlark
  public_hoist_packages = {
      "@eslint/js": [""],
      "@typescript-eslint/eslint-plugin": [""],
      "@typescript-eslint/parser": [""],
      "eslint-plugin-svelte": [""],
      "eslint-plugin-import": [""],
      "eslint-plugin-react": [""],
      "eslint-plugin-react-hooks": [""],
      "svelte-eslint-parser": [""],
      "globals": [""],
  },
  ```

#### Real-World Usage in Ducktape

The ducktape repository currently uses aspect_rules_js for:

- **Props frontend** (SvelteKit): Vite build, svelte-check, storybook, playwright visual tests
- **agent_server web** (Svelte): Vite build, svelte-check, vitest unit tests
- **rspcache admin_ui** (React): Vite build

All use cases work well for bundling and dev servers, but Playwright test runner hits the module identity issue (#1).

---

## 2. Companion Ruleset: aspect_rules_ts

**Repository:** <https://github.com/aspect-build/rules_ts>

**Current Status:** Actively maintained, recommended replacement for @bazel/typescript

#### Overview

- High-performance Bazel rules for TypeScript compiler (tsc)
- Direct replacement for the deprecated @bazel/typescript from rules_nodejs
- Layered on top of aspect_rules_js (uses its dependency management)

#### Key Features

- `ts_project` rule: thin wrapper around tsc, configuration via tsconfig.json
- First-class worker support for parallel compilation
- Support for separate transpiler (swc) for dev/incremental builds (order of magnitude faster than ts_library)
- Identical API to rules_nodejs's ts_project (easy migration)

#### Performance

- ts_project with swc transpiler: significantly faster on incremental dev builds than ts_library
- ts_library still slightly faster on full clean builds (due to worker caching) but deprecated
- Recommended for all new code

---

## 3. Framework-Specific & Toolchain Rulesets

All maintained by Aspect Build, all layer on aspect_rules_js:

### aspect_rules_esbuild

**Repository:** <https://github.com/aspect-build/rules_esbuild>

- Bazel rules for esbuild (extremely fast JS bundler)
- Can bundle JS/TS/JSX/TSX/CSS with tree-shaking and minification
- Never runs npm install (fully hermetic)
- Fetches native esbuild binary via Bazel downloader

**Key limitation:** esbuild itself leaves the sandbox by following symlinks (Issue #58)

### aspect_rules_rollup

- Bazel rules for Rollup bundler
- For projects needing Rollup-specific features (plugins, code-splitting strategies)

### aspect_rules_webpack

- Bazel rules for Webpack
- For projects with complex Webpack configurations
- Less commonly needed with modern tooling (Vite, esbuild)

### aspect_rules_swc

- Bazel rules for SWC transpiler
- Recommended for ts_project transpilation (significant speed improvement)
- Can be configured as alternative to tsc for performance-critical builds

### aspect_rules_jest

- Bazel rules for Jest testing framework
- Full TypeScript support
- Compatible with aspect_rules_js dependencies

### aspect_rules_cypress

- Bazel rules for Cypress E2E testing
- Browser-based testing integration

### aspect_rules_terser

- Bazel rules for Terser (JavaScript minification)
- Often used as final build step

---

## 4. Legacy/Deprecated Ruleset: rules_nodejs

**Repository:** <https://github.com/bazel-contrib/rules_nodejs> (bazel-contrib community maintained)

**Current Status:** Limited maintenance, recommend migrating to aspect_rules_js

#### Overview

- Original Node.js toolchain and build rules for Bazel
- Current version (6.x.x) has greatly reduced scope: only provides Node.js toolchain, **not** actual build rules
- Rules were merged into aspect_rules_js and rewritten

#### Why It's Deprecated

- Separate source/output trees broke Node.js tooling expectations
- TypeScript required hacky rootDirs pathMapping
- No workspace support without independent npm installations per package
- Inefficient: treated npm packages as thousands of individual files instead of directories
- rules_js solves all these issues with cleaner design

#### What It's Still Used For

- Providing Node.js toolchain (bazel-contrib/rules_nodejs still provides this)
- Legacy projects not yet migrated
- Historical reference/comparison

---

## 5. Alternative Implementations (Historical/Niche)

### npm-bazel (Redfin)

**Repository:** <https://github.com/redfin/npm-bazel>

- Early attempt: generator tool + Skylark rules for npm modules
- Public clone of internal Redfin code
- Not actively maintained, unstable
- Superseded by aspect_rules_js

### rules_node (pubref)

**Repository:** <https://github.com/pubref/rules_node>

- Niche implementation using yarn
- Creates external workspace `@yarn_modules` and invokes yarn install
- Not actively maintained
- Superseded by aspect_rules_js

---

## 6. Browser Testing & Playwright Integration

### rules_playwright

**Repository:** (in /home/agentydragon/code/rules_playwright)

**Purpose:** Download and manage Playwright browser binaries

#### Features

- Handles platform-specific Playwright browser downloads (Chromium, Firefox, WebKit)
- Computes and verifies integrity hashes for supply-chain security
- Supports configurable platform versions (macOS, Linux distro)
- Generates integrity maps for offline builds

#### Integration with aspect_rules_js

- Provides pre-downloaded browser binaries for Playwright tests
- Eliminates need for `npx playwright install` in sandbox
- Set `PLAYWRIGHT_BROWSERS_PATH` to binary locations

#### Known Issue: Module Identity Mismatch

- When using `playwright_bin.playwright_test()` wrapper from aspect_rules_js npm packages
- @playwright/test re-exports from playwright; symlink resolution causes same module to load twice (different paths)
- Breaks Playwright's internal test registry (uses module identity for state)
- **Workaround:** Run tests outside Bazel or use custom js_test with proper entry point configuration

---

## 7. Linting & Formatting

### aspect_rules_lint

**Repository:** <https://github.com/aspect-build/rules_lint>

**Status:** Generally Available (GA)

#### Overview

- Run linters/formatters as Bazel aspects (not separate rules)
- No BUILD file changes needed
- Works on existing js_library, ts_project targets
- Incremental: results cached in Remote Execution

#### Supported for JavaScript/TypeScript

- **ESLint:** Via configurable aspect, uses eslintrc files
- **Prettier:** Via configurable aspect, uses .prettierrc
- Can run via `bazel lint //...` (requires Aspect CLI)
- Supports `--fix` to apply fixes
- Can fail build on violations: `--@aspect_rules_lint//lint:fail_on_violation`

#### TypeScript Configuration

- For ts_project, lint the [name]\_typings label (original TS source, not transpiled output)
- ESLint configuration must include TypeScript parser (via @typescript-eslint/parser)

---

## 8. Module Resolution Issues Deep Dive

### Fundamental Challenge

Bazel keeps outputs in a distinct output tree (bazel-out), separate from sources. Node.js naturally looks in the same directory tree. This causes friction.

### aspect_rules_js Approach: Output Tree Working Directory

- Always run JS tools with working directory = Bazel output tree
- Use pnpm-style layout to create node_modules under bazel-out
- All resolutions work naturally without monkey-patching Node.js
- **Downside:** BAZEL_BINDIR must be set in env; some tools need re-pathing of inputs/outputs

### Symlink Handling

- Bazel uses symlinks extensively (efficient, avoids copies)
- Node.js follows symlinks by default
- aspect_rules_js patches fs APIs to prevent sandbox escape:
  - Patches: lstat, readlink, realpath, readdir, opendir (sync and async)
  - Default: `--preserve-symlinks=true`
  - ESM note: `--preserve-symlinks-main=true` prevents .mjs entry point escape

### Workspace Module Resolution

- pnpm workspaces resolve all members' dependencies through single lockfile
- aspect_rules_js transparently supports this
- All workspace members can be built with single npm_translate_lock rule
- Cross-workspace dependencies work: `import { X } from '@workspace/member2'`

---

## 9. Comparison Matrix

| Feature                        | aspect_rules_js            | rules_nodejs         | aspect_rules_ts            | aspect_rules_esbuild |
| ------------------------------ | -------------------------- | -------------------- | -------------------------- | -------------------- |
| **Actively Maintained**        | ✅ Yes                     | ⚠️ Limited           | ✅ Yes                     | ✅ Yes               |
| **Lazy Dependency Resolution** | ✅ Yes                     | ❌ No                | ✅ (via rules_js)          | ✅ Yes               |
| **pnpm Workspace Support**     | ✅ Yes                     | ❌ No                | ✅ (via rules_js)          | N/A                  |
| **npm Workspace Support**      | ⚠️ Limited                 | ❌ No                | ⚠️ Limited                 | N/A                  |
| **Module Resolution Correct**  | ✅ Yes                     | ❌ Requires rootDirs | ✅ Yes                     | ✅ Yes               |
| **TypeScript Compilation**     | ❌ (via rules_ts)          | ⚠️ Deprecated        | ✅ Yes                     | ✅ (via ts_project)  |
| **ESLint Support**             | ✅ (via rules_lint aspect) | ⚠️ External          | ✅ (via rules_lint aspect) | N/A                  |
| **Svelte Support**             | ✅ Works                   | ⚠️ Issues            | ✅ Works                   | ✅ Works             |
| **React Support**              | ✅ Works                   | ✅ Works             | ✅ Works                   | ✅ Works             |
| **SvelteKit Support**          | ⚠️ Dev server limitation   | ❌ No                | ✅ Works                   | ✅ Works             |
| **Next.js Support**            | ⚠️ Sandbox write issues    | ❌ No                | ⚠️ Issues                  | ⚠️ Issues            |
| **Dev Server Watch**           | ⚠️ Manual deps             | ⚠️ Manual deps       | N/A                        | N/A                  |

---

## 10. Recommended Setup for Ducktape

Given the current architecture (2 Svelte frontends, 1 React):

### Primary Stack

- **Dependency management:** `aspect_rules_js` with pnpm
- **TypeScript:** `aspect_rules_ts` (ts_project rule)
- **Bundling:** Vite (via aspect_rules_js npm packages, not dedicated rules)
- **Linting:** `aspect_rules_lint` with ESLint + Prettier aspects
- **Browser binaries:** `rules_playwright` for playwright tests
- **Framework tooling:** Let Vite/SvelteKit/build tools handle framework specifics

### For Your Specific Issues

1. **Module Resolution (Playwright test runner)**
   - Don't use `playwright_bin.playwright_test()` wrapper
   - Either: run Playwright outside Bazel via `manual` target + pnpm
   - Or: use custom `js_test` with explicit entry point configuration
   - Or: implement custom wrapper that ensures module identity consistency

2. **SvelteKit Dev Server**
   - Use `js_run_devserver` with explicit `data` deps on all source files
   - SvelteKit's Vite will rebuild on changes (if file deps correct)
   - Or: use `bazel run //props/frontend:dev` and manually reload

3. **React (rspcache admin_ui)**
   - Should work as-is with aspect_rules_js
   - Vite+React works cleanly
   - No module identity issues expected

---

## 11. Further Investigation Areas

### For Ducktape Team

1. **Playwright Module Identity Fix**
   - Investigate custom js_test wrapper that ensures single module load
   - Consider patching NODE_PATH or using require cache control
   - Evaluate if rules_playwright + custom runner better than npm packages

2. **SvelteKit Dev Experience**
   - Test if full source file glob deps eliminates rebuild issues
   - Consider symlink vs copy for dev mode
   - Evaluate ibazel for faster dev iteration

3. **ESLint Integration**
   - Set up aspect_rules_lint configuration
   - Test with all three frontends (svelte-check, eslint combo)
   - Configure hoisting for all required plugins

4. **Cross-Frontend Shared Packages**
   - Consider workspace members for shared components (if applicable)
   - Test pnpm workspace resolution through Bazel

---

## Sources

- [GitHub - aspect-build/rules_js](https://github.com/aspect-build/rules_js)
- [GitHub - aspect-build/rules_ts](https://github.com/aspect-build/rules_ts)
- [GitHub - aspect-build/rules_lint](https://github.com/aspect-build/rules_lint)
- [GitHub - aspect-build/rules_esbuild](https://github.com/aspect-build/rules_esbuild)
- [GitHub - bazel-contrib/rules_nodejs](https://github.com/bazel-contrib/rules_nodejs)
- [Bazel and JavaScript](https://bazel.build/docs/bazel-and-javascript)
- [rules_js documentation](https://docs.aspect.build/rulesets/aspect_rules_js/)
- [rules_lint documentation](https://docs.aspect.build/workflows/features/lint/)
- [pnpm Workspaces Documentation](https://pnpm.io/workspaces)
- [Ducktape Playwright Investigation](../../props/frontend/docs/playwright-bazel-investigation.md)
