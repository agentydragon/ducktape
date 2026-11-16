# Known Problems with JS Frontend Bazel Integration

These notes cover Bazel and pnpm integration issues that can affect JavaScript frontends.

## 1. Playwright Module Identity Issue

The Props Playwright-from-JS module-identity failure is summarized in the [Bazel/JS survey](exploration_bazel_js_rules.md#browser-tests-and-the-historical-module-identity-failure) and [historical investigation](../../props/frontend/docs/playwright_bazel_investigation.md). Props now uses Python Playwright visual tests; the finding remains useful for other Playwright-from-JS Bazel integrations.

## 2. Module Resolution in Bazel Sandbox with chdir

JS rules commonly use `chdir = package_name()` so tools resolve package-relative config and dependencies from their workspace member. If the pnpm workspace layout leaves `node_modules` only at the root, tools running in that member directory cannot resolve it; removing `chdir` is not a general workaround because config paths and tool lookup depend on the working directory.

## 3. Version Duplication Across package.json Files

Dependencies may be declared both at the workspace root and in member `package.json` files. Keep those manifests and the lockfile importers consistent to avoid version drift.

## 4. aspect_rules_js Workspace Detection

`aspect_rules_js` requires `pnpm-workspace.yaml` when `pnpm-lock.yaml` has importers beyond `.`. Removing workspace metadata without regenerating the lockfile leaves them inconsistent and causes lock translation to fail.
