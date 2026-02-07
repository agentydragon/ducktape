# Known Problems with JS Frontend Bazel Integration

## 1. Playwright Module Identity Issue

**Symptom**: Playwright crashes with "Playwright Test did not expect test() to be called here" and mentions "two different versions of @playwright/test".

**Root Cause**: Playwright uses JavaScript object identity to track test context state. The `playwright_bin.playwright_test` rule from aspect_rules_js creates a runner that loads `@playwright/test` from the Bazel package store. When the test file imports `@playwright/test`, it resolves to a different module instance due to how pnpm workspaces create separate node_modules symlink trees per workspace member.

**Why It Matters**: Even though both resolve to the same package version, they are different JavaScript module instances. Playwright's internal state tracking breaks when `test()` is called from a different module instance than the one the runner initialized.

**Attempted Fixes That Failed**:

- Moving @playwright/test from workspace member to root package.json
- Adding //:node_modules/@playwright/test to data deps
- Setting NODE_PATH environment variable
- Removing chdir from the rule

## 2. Module Resolution in Bazel Sandbox with chdir

**Symptom**: Tools fail with "Cannot find package 'X'" when running in sandbox.

**Root Cause**: Most JS build rules use `chdir = package_name()` to run tools in the project subdirectory (e.g., `props/frontend/`). This is needed because:

- Config files use relative paths (`./src`, `./dist`)
- Tools look for `node_modules` relative to cwd

When we flatten the pnpm workspace (single package.json at root, no workspace members), the `npm_link_all_packages` rule only creates node_modules at the root level. When the sandbox runs with chdir to a subdirectory, there's no node_modules there.

**The Tension**:

- With workspace: each project gets node_modules, but Playwright breaks due to module identity
- Without workspace: single node_modules at root, but tools can't find it from subdirectories

## 3. Version Duplication Across package.json Files

**Symptom**: Same dependency declared in both root package.json and workspace member package.json files, potentially with different versions.

**Root Cause**: pnpm workspaces require each project to declare its own dependencies. The root package.json may also have shared deps. This creates:

- Maintenance burden (update in multiple places)
- Risk of version drift
- Confusion about source of truth

**aspect_rules_js Behavior**: The `npm_translate_lock` rule reads the lockfile and creates Bazel targets. It expects workspace member package.json files if the lockfile references workspace packages.

## 4. Storybook + Vite Version Constraints

**Symptom**: Storybook build fails with peer dependency errors.

**Root Cause**: Storybook 8.x requires vite ^4.0.0 || ^5.0.0 || ^6.0.0. Cannot use vite 7.x.

**Current Workaround**: Pinned vite to 6.3.5, @sveltejs/vite-plugin-svelte to 5.1.0.

**Status**: Resolved, but constrains vite upgrades until Storybook catches up.

## 5. aspect_rules_js Workspace Detection

**Symptom**: Bazel fails with "expected pnpm-workspace.yaml to exist since the pnpm-lock.yaml file contains workspace packages".

**Root Cause**: aspect_rules_js parses pnpm-lock.yaml and detects workspace package references in the `importers` section. If it finds entries other than `.` (root), it requires pnpm-workspace.yaml to exist.

**Implication**: Can't just delete pnpm-workspace.yaml without regenerating the lockfile from scratch. The lockfile and workspace config must be consistent.
