# JavaScript Bundlers and Bazel Integration Exploration

**Document Status**: Research findings compiled for decision-making on bundler strategy for Svelte and React frontends.

**Context**: We're evaluating whether to continue with Vite or explore alternative bundlers (esbuild, Rollup, webpack, Parcel, swc, Rspack) for building three JS frontends (props/frontend, agent_server/web, rspcache/admin_ui) under Bazel.

**Core Problem**: Module identity issues with Playwright when using pnpm workspaces, and module resolution complexity in sandboxed Bazel builds.

---

## Executive Summary

| Bundler       | Bazel Rules                                                    | Svelte Support                          | React Support              | Module Resolution        | Sandbox-Friendly | Maturity         | Notes                                                           |
| ------------- | -------------------------------------------------------------- | --------------------------------------- | -------------------------- | ------------------------ | ---------------- | ---------------- | --------------------------------------------------------------- |
| **Vite**      | Third-party via npm                                            | Excellent (native)                      | Good                       | Via npm packages         | Problematic      | High             | Current choice; works but has Playwright identity issues        |
| **esbuild**   | [rules_esbuild](https://github.com/aspect-build/rules_esbuild) | Via plugin (esbuild-svelte)             | Good                       | Direct filesystem        | Excellent        | High             | 10-100x faster; lacks plugins for advanced features             |
| **Rollup**    | [rules_rollup](https://github.com/aspect-build/rules_rollup)   | Via plugin                              | Good                       | Configurable             | Good             | High             | Flexible plugin API; slower than esbuild; used by Vite for prod |
| **webpack**   | [rules_webpack](https://github.com/aspect-build/rules_webpack) | Via plugin/loader                       | Excellent                  | Most flexible            | Good             | Mature but heavy | Most configuration overhead; in early Bazel development         |
| **Parcel**    | Minimal/custom only                                            | Via parcel-plugin-svelte (unmaintained) | Good                       | Zero-config but limited  | Unknown          | Lower            | Zero-config philosophy conflicts with Bazel's explicit model    |
| **swc**       | Via Rspack mainly                                              | Limited directly (via Rspack)           | Via SWC transformer        | Via swcpack (has issues) | Unknown          | Medium           | Rust-based transpiler; bundler (swcpack) has bugs               |
| **Rspack**    | Not directly integrated                                        | Via @rsbuild/plugin-svelte              | Via @rsbuild/plugin-react  | Direct filesystem        | Unknown          | Lower            | New; webpack-compatible; 10x faster; focused on Next.js         |
| **Turbopack** | Architectural inspiration only                                 | Unknown                                 | Excellent (Next.js native) | Unknown                  | Unknown          | Early            | Rust-based successor to webpack; no direct Bazel integration    |

---

## Detailed Bundler Analysis

### 1. Vite (Current Choice)

**Bazel Integration**: Via npm package, no native Bazel rules
**Repository**: <https://github.com/vitejs/vite>

#### Strengths

- **Native Svelte support**: SvelteKit is built on Vite; excellent developer experience
- **Dual strategy**: esbuild for dev (fast), Rollup for prod (flexible)
- **Modern architecture**: ES modules + HMR designed from the ground up
- **Excellent hot module replacement**: Only invalidates changed module boundaries
- **Zero-config philosophy for common cases**: Works well with SvelteKit

#### Weaknesses

- **Bazel integration via npm**: Not designed for hermetic builds; requires pnpm package manager
- **Module identity problem with Playwright**: pnpm workspace creates separate node_modules trees per project, causing Playwright to load from different paths
- **Vite 7 incompatibilities**: Storybook 8.x doesn't support vite 7.x yet
- **pnpm-specific**: Most examples and integrations assume pnpm workspaces
- **Dev server complexity**: js_run_devserver adds configuration layer in Bazel

#### Known Issues with Current Setup

1. **Playwright module identity crash**: "Playwright Test did not expect test() to be called here" when same package loads from different paths
2. **Version duplication**: Dependencies declared in both root and per-project package.json
3. **Workspace node_modules complexity**: pnpm creates symlink trees that don't play well with Bazel sandboxing

#### Vite + esbuild + Rollup Decision

Vite uses **esbuild for development** (fast rebuilds, ES module handling) and **Rollup for production** (flexible plugins, better code splitting). The plugin APIs are incompatible—Vite's adoption of Rollup's flexible plugin system was key to its ecosystem success. There's an ongoing effort to build Rolldown (Rust port of Rollup) which could eventually replace both.

---

### 2. esbuild

**Bazel Integration**: [aspect-build/rules_esbuild](https://github.com/aspect-build/rules_esbuild)
**Written in**: Go
**Repository**: <https://github.com/evanw/esbuild>

#### Strengths

- **Extreme speed**: 10-100x faster than webpack; wrote in Go (native code, not JS)
- **Hermetic Bazel integration**: rules_esbuild downloads and manages esbuild binary independently; doesn't require npm
- **Sandbox-friendly**: Custom resolver plugin prevents escape from Bazel sandbox via symlinks
- **Minimal configuration**: Simpler API than webpack
- **Active ecosystem**: esbuild-svelte plugin maintains cache for watch mode
- **No npm install required**: Fully self-contained toolchain via Bazel downloader

#### Weaknesses

- **Limited plugin system**: Plugin API is newer and less mature than Rollup/webpack
- **No template/framework-specific loaders**: Requires external plugins for Svelte, Vue, JSX
- **Code splitting less advanced**: No dynamic import() optimization like Rollup
- **Svelte requires external plugin**: Must use esbuild-svelte (maintained by community, not Svelte team)
- **No first-class React/JSX**: Requires configuration vs. webpack/Vite's automatic handling

#### Svelte Compilation

Use `esbuild-svelte` plugin:

- Supports TypeScript in .svelte files natively
- Caching support for watch/incremental builds
- Must configure mainFields = ["svelte", "browser", "module", "main"]
- Must add "svelte" to conditions for proper exports resolution

#### React Compilation

- Built-in JSX support (target es2020+)
- No special configuration needed for basic React
- But missing some ecosystem integrations (React Fast Refresh, etc. require setup)

#### Bazel Examples

- `/tmp/rules_esbuild/examples/`: CSS, splitting, format variations, macros, plugins, targets
- CSS support via config: `keepNames`, `resolveExtensions`
- Plugin system allows custom behavior (see `/examples/plugins/`)

#### Module Resolution

- **Direct filesystem access**: esbuild resolves directly against node_modules
- **Works well with aspect_rules_js**: npm_link_all_packages provides node_modules layout
- **No module identity issues**: Single instance of packages in the output tree

---

### 3. Rollup

**Bazel Integration**: [aspect-build/rules_rollup](https://github.com/aspect-build/rules_rollup)
**Repository**: <https://github.com/rollup/rollup>

#### Strengths

- **Flexible plugin ecosystem**: Most mature plugin API in JavaScript bundlers
- **Tree-shaking**: Originally designed for libraries; still excels at dead code elimination
- **Used by Vite for production**: Proven battle-tested in high-traffic sites
- **Code splitting**: Better than esbuild for complex splitting scenarios
- **Output formats**: amd, cjs, esm, iife, umd, system
- **Bazel integration**: Pure Starlark implementation for aspect_rules_js compatibility

#### Weaknesses

- **Slower than esbuild**: JavaScript-based; ~10x slower for bundling
- **Plugin complexity**: Requires more setup than esbuild for basic tasks
- **Not optimized for dev mode**: Mainly intended for production builds
- **Less intuitive for monorepos**: Requires explicit configuration for each workspace member

#### Svelte Compilation

- Via rollup-plugin-svelte
- More mature than esbuild-svelte (Svelte team maintains it)
- Better CSS handling within Svelte ecosystem

#### Bazel Usage Pattern

Typically paired with esbuild for dev, Rollup for prod—exactly like Vite.

---

### 4. webpack

**Bazel Integration**: [aspect-build/rules_webpack](https://github.com/aspect-build/rules_webpack) (early development)
**Repository**: <https://github.com/webpack/webpack>

#### Strengths

- **Most feature-complete**: Can be configured for nearly any scenario
- **Ecosystem maturity**: Longest-standing bundler; most documentation/examples
- **Advanced code splitting**: Fine-grained control over chunk boundaries
- **React ecosystem**: Best integration with React tools (Create React App, Next.js historically)
- **Module federation**: Built-in support for federated modules (Micro Frontends)

#### Weaknesses

- **Configuration complexity**: Notoriously verbose; steep learning curve
- **Slow rebuilds**: 500ms-1.6s vs. Vite's 10-20ms HMR
- **Cold start penalty**: ~7s vs. Vite's ~1.2s
- **Bazel integration immature**: rules_webpack v0.17.1 is early development; may have breaking changes
- **Heavy**: Large npm package; many dependencies
- **Less relevant for modern dev**: Vite/esbuild movement away from webpack

#### Bazel Status

- Early development (v0.17.1, released April 2025)
- Provides webpack_bundle and webpack_devserver
- Still stabilizing API

---

### 5. Parcel

**Bazel Integration**: Custom rules only; no official integration
**Repository**: <https://github.com/parcel-bundler/parcel>

#### Strengths

- **Zero-config philosophy**: Works with Svelte, React, TypeScript out of the box
- **Simple mental model**: Specify entry points, get bundles
- **Fast for small projects**: Built-in esbuild usage for production
- **React integration**: Good Fast Refresh support

#### Weaknesses

- **Zero-config conflicts with Bazel**: Bazel's philosophy is explicit configuration
- **Svelte support unmaintained**: parcel-plugin-svelte last updated 4 years ago
- **Limited Bazel examples**: Only basic custom rule examples in rules_nodejs
- **Less Bazel adoption**: Smaller ecosystem in Bazel community
- **No official Bazel rules**: Would need custom wrapper

#### Assessment for Our Use Case

Parcel's zero-config approach works against Bazel's explicit model. The unmaintained Svelte plugin is a red flag.

---

### 6. swc

**Bazel Integration**: Minimal; mainly via Rspack
**Repository**: <https://github.com/swc-project/swc>

#### Key Points

- **Transpiler**: swc is primarily a fast JavaScript/TypeScript transpiler (Rust-based), not a bundler
- **Bundler attempt**: swcpack (or spack) exists but has known issues with external dependencies
- **Ecosystem role**: Powers tools like Next.js, Deno, Parcel; used in Rspack
- **Known swcpack bugs**: Users report errors when bundling modules with external dependencies

#### Why swcpack is Problematic

swcpack doesn't handle npm dependencies well. Users attempting complex projects report failures when importing external packages.

#### Relevant for Us

swc is valuable as a **transpiler component** inside other bundlers, but not as a standalone bundler choice.

---

### 7. Rspack

**Bazel Integration**: Not directly; webpack-compatible
**Repository**: <https://github.com/web-infra-dev/rspack>

#### Overview

Rust-based bundler, 10x faster than webpack, webpack-compatible API.

#### Strengths

- **Speed**: Rust implementation; 10x faster than webpack
- **webpack compatibility**: Drop-in replacement for webpack in many cases
- **Svelte support**: @rsbuild/plugin-svelte
- **React support**: @rsbuild/plugin-react
- **Built on esbuild/SWC**: Uses SWC transformer for JSX/TSX

#### Weaknesses

- **Bazel integration uncertain**: No direct integration mentioned; webpack-compatible but rules_webpack is early-stage
- **Still evolving**: Newer project; ecosystem less established
- **Rsbuild wrapper**: Often used via Rsbuild (higher-level abstraction) rather than directly
- **Less Bazel adoption**: Small community in Bazel space

#### Bazel Path

Would need to use rules_webpack if targeting Bazel, since Rspack is webpack-compatible.

---

### 8. Turbopack

**Bazel Integration**: Architectural inspiration only; no direct rules
**Repository**: <https://github.com/vercel/turbo> (includes Turbopack)

#### Overview

Next-gen Rust-based bundler from webpack creator; learned from Bazel's incremental computation model.

#### Key Point

Turbopack's design borrows from Bazel's philosophy of "never do the same work twice" and incremental caching. However, **there is no Bazel integration for Turbopack**. It's designed as a standalone bundler/build system in its own right.

#### Why It's Not an Option

- Tightly coupled to Next.js ecosystem
- No Svelte support
- Not designed for monorepo scenarios outside of Next.js
- Architectural inspiration to Bazel, but not a Bazel integration

---

## Module Identity Problem (Playwright Issue)

### Root Cause

When using pnpm workspaces with aspect_rules_js, each workspace member gets its own `node_modules` symlink tree. This means:

1. `/props/frontend/node_modules/@playwright/test` → symlink to pnpm store
2. `/agent_server/web/node_modules/@playwright/test` → symlink to same package, different path
3. JavaScript module loading: Both resolve to the same package version, but are **different module instances**

### Why Playwright Breaks

Playwright's test framework uses JavaScript object identity to track execution context. When `test()` is called, it checks if the caller is the same module instance that was initialized. pnpm workspaces violate this assumption—the test runner initializes one instance, but the test file imports from a different path, creating a second instance.

### Potential Solutions

1. **Flatten workspace** (aspect_rules_js supported):
   - Single `package.json` at root, no workspace members
   - Single node_modules tree
   - **Trade-off**: Module resolution from subdirectories becomes complex (need NODE_PATH hacks)

2. **Use hardlinks instead of symlinks**:
   - pnpm's `dependenciesMeta[].injected` option or `hoist=false + hard-link` mode
   - **Status**: Not confirmed to fix Playwright identity issue; mainly a deduplication strategy

3. **Monolithic test runner** (no per-workspace test):
   - Keep Playwright tests only in root or single location
   - All tests share same node_modules instance
   - **Trade-off**: Less modular testing; harder to organize

4. **Build bundled test file**:
   - Compile/bundle test files with esbuild/Rollup before running
   - Single module instance in bundle
   - **Pro**: Works; honest solution
   - **Con**: Extra build step; breaks dev workflow

5. **Use different test framework**:
   - Vitest, Jest, etc. may not have same module identity requirements
   - **Status**: Vitest already in agent_server/web; no Playwright identity issues reported

6. **Module federation/separate process**:
   - Run Playwright in isolated process; inject test context via IPC
   - **Status**: Complex; not a standard pattern

### Current Workaround

For agent_server/web: Use Vitest for unit tests, avoid Playwright under Bazel, or run Playwright outside Bazel build system.

### Bundler Impact

**esbuild and Rollup**: Both work well with aspect_rules_js and can be configured to use a single node_modules tree. Using a hermetic bundler (esbuild via rules_esbuild) avoids the symlink problem entirely because the bundler operates directly on the output tree, not the source tree.

---

## Bazel-Specific Considerations

### rules_js (aspect_rules_js) Architecture

**Key insight from aspect_rules_js documentation:**

> "rules_js always runs JS tools with the working directory in Bazel's output tree. It uses a pnpm-style layout tool to create a node_modules under bazel-out, and all resolutions naturally work."

This means:

- Source files and node_modules are co-located in bazel-out
- Node.js module resolution works naturally
- No monkey-patching of require() needed
- No symlink issues (files, not symlinks, in sandbox)

### Workspace Detection

aspect_rules_js parses `pnpm-lock.yaml` and detects workspace package references in the `importers` section. If it finds entries other than `.` (root), it requires `pnpm-workspace.yaml` to exist. This creates a coupling: can't delete `pnpm-workspace.yaml` without regenerating lockfile.

### Module Resolution Strategies

#### With Vite (current)

- Vite runs from workspace member directory with `chdir = package_name()`
- Looks for node_modules relative to cwd
- pnpm workspace creates separate symlink trees per member
- **Problem**: Playwright loads from different symlink paths

#### With esbuild + rules_esbuild

- esbuild rule has access to all sources and node_modules
- No chdir needed (esbuild works in execroot)
- Direct filesystem access, no symlinks
- **Advantage**: Single module instance across entire build

#### Best Practice

Use hermetic, Bazel-native rules (rules_esbuild, rules_rollup) rather than relying on npm-based tools. This gives better sandbox compatibility and avoids symlink complexity.

---

## Recommendations

### Option 1: Continue with Vite (Safest, Requires Workarounds)

**Pros**:

- Excellent Svelte DX
- Native SvelteKit support for props/frontend
- Existing Bazel integration (working, even if imperfect)
- Team familiarity

**Cons**:

- Playwright module identity issue unresolved
- Must work around via Vitest or custom solutions
- pnpm workspace complexity
- Not leveraging Bazel's hermetic build model

**When to choose**: If Playwright testing under Bazel is not critical; team values Svelte DX highly.

---

### Option 2: Migrate to esbuild + Rollup (Recommended for Hermetic Builds)

**Setup**:

- **Dev & unit tests**: esbuild via rules_esbuild (fast)
- **Production**: Rollup via rules_rollup (flexible plugins)
- **Svelte**: esbuild-svelte plugin for dev, rollup-plugin-svelte for prod
- **React**: Native JSX support (minimal config)

**Pros**:

- True hermetic builds (no npm install)
- Eliminates symlink/module identity issues
- esbuild speed (dev experience improvement)
- Explicit Bazel control
- Single node_modules tree (no workspace symlink trees)

**Cons**:

- Must configure Svelte compilation explicitly
- Less polished Svelte DX than Vite
- No built-in HMR (js_run_devserver handles it)
- More configuration

**Complexity**: Medium (esbuild-svelte plugin adds configuration)
**Bazel alignment**: Excellent

**When to choose**: If Playwright/testing robustness is priority; comfortable with lower-level bundler config.

---

### Option 3: Flatten pnpm Workspace (Structural Fix)

**Change**:

- Merge all package.json into root
- Single pnpm-workspace.yaml
- Single node_modules tree at repo root

**Pros**:

- Eliminates symlink complexity
- Playwright module identity resolved
- Simpler to reason about
- Potential for shared code reuse

**Cons**:

- More boilerplate for dependency declarations
- Harder to keep frontends independent
- Requires chdir workarounds or restructuring tools
- pnpm hoisting may create phantom dependencies

**When to choose**: If you want to merge frontends into closer monorepo integration anyway.

---

### Option 4: Mixed Approach (Pragmatic)

**Setup**:

- Keep Vite for props/frontend (SvelteKit, stable)
- Migrate agent_server/web to esbuild (smaller, fewer deps)
- rspcache/admin_ui with esbuild or webpack (React-only, simpler)
- Use Vitest for all unit tests (avoids Playwright identity issues)
- Keep Playwright tests outside Bazel (run separately)

**Pros**:

- Incremental migration; low risk
- Can prove esbuild approach on smaller project first
- Leverage Vite's SvelteKit support where it's best
- Test complexity stays manageable

**Cons**:

- Multiple bundler configurations to maintain
- Inconsistent developer experience across frontends
- Technical debt if not eventually unified

**When to choose**: If you want to de-risk the migration; proving ground for alternatives.

---

## Comparison Matrix: Bazel Bundler Readiness

| Factor                         | esbuild       | Rollup    | webpack      | Vite           | Parcel          | Rspack               |
| ------------------------------ | ------------- | --------- | ------------ | -------------- | --------------- | -------------------- |
| **Official Bazel Rules**       | ✅ Yes        | ✅ Yes    | ⚠️ Early     | ❌ No          | ❌ No           | ❌ No                |
| **Hermetic Build**             | ✅ Excellent  | ✅ Good   | ✅ Good      | ❌ Via npm     | ❌ Zero-config  | ⚠️ Via webpack rules |
| **Svelte Support**             | ✅ Plugin     | ✅ Plugin | ✅ Plugin    | ✅ Native      | ⚠️ Unmaintained | ✅ Plugin            |
| **React Support**              | ✅ Native JSX | ✅ Good   | ✅ Excellent | ✅ Good        | ✅ Good         | ✅ Good              |
| **Speed**                      | ⭐⭐⭐⭐⭐    | ⭐⭐⭐    | ⭐⭐         | ⭐⭐⭐⭐       | ⭐⭐⭐⭐        | ⭐⭐⭐⭐             |
| **Dev Experience**             | ⭐⭐⭐        | ⭐⭐      | ⭐⭐         | ⭐⭐⭐⭐⭐     | ⭐⭐⭐⭐⭐      | ⭐⭐⭐⭐             |
| **Sandbox Friendliness**       | ✅ Excellent  | ✅ Good   | ✅ Good      | ⚠️ Problematic | ❓ Unknown      | ⚠️ Unknown           |
| **Configuration Burden**       | ✅ Low        | ⭐⭐      | ⭐⭐⭐⭐     | ✅ Low         | ✅ Zero-config  | ⭐⭐                 |
| **Playground identity issues** | ✅ No         | ✅ No     | ✅ No        | ❌ Yes         | ❌ Yes          | ❌ Yes               |

---

## Repository References

Example implementations and documentation:

- **rules_esbuild**: <https://github.com/aspect-build/rules_esbuild>
  - `/examples/`: CSS, splitting, macros, plugins, targets
  - `/docs/rules.md`: API documentation
- **rules_rollup**: <https://github.com/aspect-build/rules_rollup>
  - `/example/`: Example project
  - `/docs/rollup.md`: API documentation

- **rules_webpack**: <https://github.com/aspect-build/rules_webpack>
  - Early development; API may change
  - `/docs/rules.md`: webpack_bundle, webpack_devserver

- **Svelte + Bazel Example**: <https://github.com/thelgevold/svelte-bazel-example>
  - Uses rules_rollup with Rollup for bundling
  - Custom Svelte build rules

- **esbuild-svelte Plugin**: <https://github.com/EMH333/esbuild-svelte>
  - Compiles .svelte files for esbuild
  - Caching support for watch mode

---

## Next Steps

1. **Decide on primary bundler** based on priority:
   - Hermetic builds + Playwright robustness → esbuild
   - SvelteKit + DX → Vite (accept workarounds)
   - Flexibility + React ecosystem → webpack (but pay config cost)

2. **Prototype on smaller frontend** (agent_server/web or rspcache/admin_ui) before full migration

3. **Document bundler-specific patterns** in each frontend's AGENTS.md once chosen

4. **Plan Playwright testing strategy**:
   - esbuild path: Run Playwright outside Bazel or via single-instance approach
   - Vite path: Use Vitest for Bazel tests, Playwright for CI-only integration tests

5. **Consider workspace structure** once all bundlers are settled

---

## Sources

- [aspect-build/rules_esbuild](https://github.com/aspect-build/rules_esbuild)
- [aspect-build/rules_js](https://github.com/aspect-build/rules_js)
- [aspect-build/rules_rollup](https://github.com/aspect-build/rules_rollup)
- [aspect-build/rules_webpack](https://github.com/aspect-build/rules_webpack)
- [esbuild Official](https://esbuild.github.io)
- [Vite Official](https://vite.dev)
- [Rollup Official](https://rollupjs.org)
- [pnpm Documentation](https://pnpm.io)
- [esbuild-svelte Plugin](https://github.com/EMH333/esbuild-svelte)
- [thelgevold/svelte-bazel-example](https://github.com/thelgevold/svelte-bazel-example)
- [Bazel JavaScript Documentation](https://bazel.build/docs/bazel-and-javascript)
