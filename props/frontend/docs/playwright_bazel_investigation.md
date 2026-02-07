# Playwright + Bazel Module Resolution Investigation

## Problem

When running Playwright tests via Bazel using `playwright_bin.playwright_test` from aspect_rules_js,
the test fails with:

```
Error: Playwright Test did not expect test() to be called here.
Most common reasons include:
- You have two different versions of @playwright/test. This usually happens
  when one of the dependencies in your package.json depends on @playwright/test.
```

The test works fine when run directly via `pnpm playwright test`.

## Root Cause

The issue is **module identity mismatch**: the Playwright CLI runner and the test file's imports
resolve `@playwright/test` to physically different module instances, even though they're the same
version (1.57.0). Playwright's internal registry uses module identity to track test context, so
when the test file's `test()` function is called, it's from a different module instance than what
the runner set up.

## Package Structure

```
@playwright/test@1.57.0  (re-exports from playwright/test)
    └── playwright@1.57.0
            └── playwright-core@1.57.0
```

The `@playwright/test/index.js` does:

```javascript
module.exports = require("playwright/test");
```

This means `@playwright/test` and `playwright` must resolve to the same physical module for the
registry to work.

## How aspect_rules_js Structures node_modules

aspect_rules_js uses a "package store" approach:

```
node_modules/
├── .aspect_rules_js/
│   ├── @playwright+test@1.57.0/
│   │   └── node_modules/
│   │       ├── @playwright/test/  (actual package files)
│   │       └── playwright -> ../../playwright@1.57.0/node_modules/playwright
│   └── playwright@1.57.0/
│       └── node_modules/
│           └── playwright/  (actual package files)
├── @playwright/
│   └── test -> ../.aspect_rules_js/@playwright+test@1.57.0/node_modules/@playwright/test
└── playwright -> .aspect_rules_js/playwright@1.57.0/node_modules/playwright
```

## The Module Resolution Conflict

### Where the CLI Loads From

The `playwright_bin.playwright_test` wrapper uses entry point:

```
node_modules/.aspect_rules_js/@playwright+test@1.57.0/node_modules/@playwright/test/cli.js
```

When this CLI's internal code requires `playwright`, it resolves to:

```
node_modules/.aspect_rules_js/@playwright+test@1.57.0/node_modules/playwright
  → symlinks to ../../playwright@1.57.0/node_modules/playwright
```

### Where the Test File Loads From

The test file at `props/frontend/tests/visual-regression.spec.ts` imports:

```typescript
import { test, expect } from "@playwright/test";
```

Playwright's transform system compiles this. The resolution depends on:

1. The `chdir` setting (we tried with and without)
2. The `props/frontend/node_modules` directory existing (even if it doesn't have @playwright)
3. NODE_PATH settings

The test file should find `@playwright/test` at `node_modules/@playwright/test` (the symlink),
which points to the same physical location as the CLI uses. BUT Node.js caches modules by their
**resolved filesystem path**, and symlinks can cause different cache keys.

## Attempts Made

### Attempt 1: Move @playwright/test to workspace root package.json

**Hypothesis**: If `@playwright/test` is only in root package.json (not `props/frontend/package.json`),
both CLI and test file will use the same module.

**Result**: Failed. Still got the module mismatch error.

**Files changed**:

- `/package.json` - added `@playwright/test`
- `/props/frontend/package.json` - removed `@playwright/test`
- `/props/frontend/BUILD.bazel` - changed load to `@npm_ducktape//:@playwright/test/package_json.bzl`

### Attempt 2: Add //:node_modules/@playwright/test to data deps

**Hypothesis**: Explicitly including the root node_modules target will make it available.

**Result**: The symlink was created correctly in runfiles, but module mismatch persisted.

**Files changed**:

- Added `//:node_modules/@playwright/test` to `data` in BUILD.bazel

### Attempt 3: Remove chdir

**Hypothesis**: Running from workspace root instead of `props/frontend` will use consistent resolution.

**Result**: Failed. Same error.

**Files changed**:

- Removed `chdir = package_name()` from visual_test
- Added `--config props/frontend/playwright.config.ts` to args

### Attempt 4: Set NODE_PATH

**Hypothesis**: Setting `NODE_PATH=node_modules` will force Node to find packages there first.

**Result**: Failed. Same error.

**Files changed**:

- Added `NODE_PATH: "node_modules"` to env

## Debugging Commands Used

### Check runfiles structure

```bash
ls -la $BAZEL_CACHE/execroot/_main/bazel-out/k8-fastbuild/bin/props/frontend/visual_test_/visual_test.runfiles/_main/node_modules/
```

### Check symlink targets

```bash
ls -la $BAZEL_CACHE/.../node_modules/@playwright/test
ls -la $BAZEL_CACHE/.../node_modules/.aspect_rules_js/@playwright+test@1.57.0/node_modules/
```

### Verify test works outside Bazel

```bash
cd props/frontend && pnpm playwright test --list
```

### Read test wrapper script

```bash
cat $BAZEL_CACHE/.../visual_test_/visual_test | grep entry_point
```

## Additional Factor: @storybook/test-runner

The `@storybook/test-runner` package (in `props/frontend/package.json`) has its own dependency on
`playwright@1.57.0`. This could contribute to module duplication, but removing it isn't an option
as it may be used for other testing.

## Potential Solutions (Not Yet Tried)

### 1. Use js_test with Explicit Entry Point

Instead of `playwright_bin.playwright_test`, use `js_test` with:

- `entry_point` pointing to the package-local playwright cli
- Manual NODE_OPTIONS to prevent module caching issues

### 2. Patch the Test File Import

Change the test file to use a relative import that definitively points to the same module:

```typescript
// Instead of:
import { test, expect } from "@playwright/test";
// Use:
import { test, expect } from "../node_modules/@playwright/test";
```

This is hacky and may break IDE tooling.

### 3. Investigate patch_node_fs

aspect_rules_js sets `JS_BINARY__PATCH_NODE_FS=1` by default. This patches Node's fs module to
handle Bazel's symlink structure. This might be conflicting with Playwright's internal module
resolution.

Try: `patch_node_fs = False` in the playwright_bin rule (if supported).

### 4. Use Native Playwright Install

Instead of using aspect_rules_js npm packages, install Playwright browsers and package globally
or use `rules_playwright` more directly.

### 5. Single-Process Approach

Use a shell script that:

1. cd to a directory with properly structured node_modules
2. Runs playwright directly using the package's cli
3. Avoids the aspect_rules_js wrapper entirely

## Current Workaround

The test is marked `manual` and can be run via:

```bash
cd props/frontend && pnpm playwright test
```

## Key Insight

The fundamental issue is that Playwright uses module identity (object reference equality) to track
its internal state. When the test runner loads `@playwright/test` and sets up the test context,
it expects test files to import from the _exact same module instance_. In Bazel's sandboxed
environment with symlinked node_modules, achieving this module identity match is challenging.

## References

- Playwright source: `lib/common/testType.js` - `_currentSuite` check
- aspect_rules_js: `bin_test_internal` and `js_binary.sh.tpl`
- Node.js module resolution: https://nodejs.org/api/modules.html#all-together
