# Plan: Excalidraw live-browser smoke

Build an open-source live-browser smoke test for the debundler against
a Bazel-managed Excalidraw bundle. The motivation: when a debundler
issue surfaces against a private upstream corpus (proxy crash, AST
corruption, missing chunk, emit shape regression, optimisation
behaviour bug), reproducing the failure on Excalidraw lets us share
the repro in a public bug report, write a regression test that runs
in ducktape's open CI, and avoid leaking proprietary upstream bundle
detail. Excalidraw is open-source and broadly representative of "real
React + vendored chunks + dynamic imports + service worker."

## Bundle build

Landed: <../excalidraw/README.md> (esbuild production build of an app entry
around the npm-pinned package, residual-baseline `debundle_pipeline`, shape and
cost). Remaining: everything below.

## Spec scope

Landed: <../excalidraw/README.md> § Spec (source-anchored modules in two chunks,
whole-chunk and partial vendor swaps, renames through module members). Still to
add when the engine supports them: `named_from_default`,
`named_from_module_default` and JSON-default swap shapes (none occurs in this
esbuild output), and `emit_browser_harness` for the smoke below.

## Smoke target contract

Landed: `//devinfra/js/debundle/excalidraw:test_smoke` (<../excalidraw/README.md>
§ Spec). Not yet covered: lazy chunks reached by `import()` after startup
(locales, font subsetting, file system), and a published screenshot.

## Hosting

Self-hosted, no MITM. Private-corpus smokes generally have to MITM the
live host because their auth/data is server-side; Excalidraw runs
entirely in the browser, so a self-hosted bundle is a fully working
app. Self-hosting removes the network dependency and CDN-rotation
flakiness (the test stays green even when excalidraw.com is down) and
matches the "reproduce against Excalidraw" workflow goal — a public,
deterministic smoke that doesn't depend on third-party uptime.

## Workflow rule

When a private-corpus debundler issue is tractable on Excalidraw too,
prefer landing the regression test on this Excalidraw target (or a
smaller minimised e2e under `devinfra/js/debundle/e2e/`) rather than
only fixing it behind the private repo. The latter loses the
public-CI signal and the public-bug-report leverage.
