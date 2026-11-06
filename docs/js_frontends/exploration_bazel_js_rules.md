# JavaScript/TypeScript rules in Ducktape

The repository uses `aspect_rules_js` for npm dependencies, `aspect_rules_ts`
through local macros for compilation, and `rules_nodejs` for the Node toolchain.
This page records local choices; the earlier upstream ruleset survey is superseded
by the implementations linked below.

## Dependencies and toolchains

[MODULE.bazel](../../MODULE.bazel) owns the ruleset, Node, TypeScript and pnpm pins,
`npm_translate_lock`, workspace inputs and ESLint plugin hoisting. Read that
configuration rather than copying a standalone upstream setup.

- Root [package.json](../../package.json), [pnpm-workspace.yaml](../../pnpm-workspace.yaml)
  and [pnpm-lock.yaml](../../pnpm-lock.yaml) define the shared workspace. New workspace
  manifests must also be inputs to `npm_translate_lock`.
- Bazel updates the lockfile after dependency edits; follow the root
  [AGENTS.md](../../AGENTS.md#javascript--typescript) workflow. Do not run `pnpm install`.
- BUILD dependencies name exact `//:node_modules/<package>` targets. Runtime plugin
  discovery may additionally require hoisting in `MODULE.bazel`.
- Bundlers consume the emitted JavaScript; see the
  [Props bundle](../../props/frontend/BUILD.bazel) for esbuild integration.

## Compilation and specs

[STYLE.md](../../STYLE.md#typescript) defines the TypeScript contract. The local
[ts_library](../../devinfra/js/ts_library.bzl) macro deliberately differs from stock
`ts_project`: it generates a config whose `files` are exactly the target's `srcs`,
extends the shared `ts_config`, and uses `tsc` for both checking and emission.
Substituting SWC or esbuild here would remove the type check.

With `isolated_typecheck`, checking moves to `<name>_typecheck`; building only the
library no longer checks types. CI includes those generated targets. This opt-in
requires `isolatedDeclarations` and explicit types on exported symbols.

The [Vitest macros](../../devinfra/js/vitest.bzl) compile each spec through
`ts_library` and run the emitted JavaScript. They generate the package config;
`check_vitest_specs()` detects unlisted specs within that Bazel package, so each
package containing specs needs the check. Environment choices and DOM exceptions
are specified in [STYLE.md](../../STYLE.md#frontend-specs-vitest).

## Lint and formatting

[.bazelrc](../../.bazelrc) enables the repository's
[lint aspects](../../devinfra/lint/linters.bzl) on builds, including ESLint with
findings treated as failures. [eslint.config.js](../../eslint.config.js) is the
flat configuration; its project globs determine which frontend sources are covered.
Prettier runs through the repository's [pre-commit configuration](../../.pre-commit-config.yaml).

## Browser tests and the historical module-identity failure

[MODULE.bazel](../../MODULE.bazel) pins Chrome for Testing's headless shell;
[the browser launcher](../../util/testing/browser/launcher.py) supplies it from
runfiles. Review screenshots use the
[Python visual-test harness](../../util/testing/frontend_visual/README.md#one-way-in)
and its deterministic context and capture API. Browser tests run on RBE, as required
by [AGENTS.md](../../AGENTS.md#bazel-commands).

The [Props Playwright investigation](../../props/frontend/docs/playwright_bazel_investigation.md)
retains the original module-identity failure and unsuccessful experiments:
the JS runner and test imports resolved different instances of `@playwright/test`,
breaking its shared test registry. Props now uses Python Playwright visual tests;
the investigation's manual/pnpm workaround describes that historical setup and is
not the current browser-test workflow. It remains relevant when diagnosing a
Playwright-from-JS integration.
