# Playwright + Bazel Module Identity Failure

Historical finding: Props now runs browser checks through Python Playwright visual tests in
[`BUILD.bazel`](../BUILD.bazel). This note records a failure mode that may help other Bazel
frontends using Playwright from JavaScript.

Under `aspect_rules_js`'s `playwright_bin.playwright_test`, the test failed with:

```text
Error: Playwright Test did not expect test() to be called here.
```

The same test worked with `pnpm playwright test`. The Bazel CLI and the transformed test import
resolved `@playwright/test` to separate physical module instances, even though both were version
`1.57.0`. The package re-exports `playwright/test`, whose test registry is module state; the runner
and test must load the same instance. With separate instances, the test's `test()` call cannot see
the active suite context set up by the runner and triggers the error. Matching versions alone does not ensure
matching module identity.

Relevant implementation details are the `_currentSuite` check in Playwright's
`lib/common/testType.js`, the `bin_test_internal` and `js_binary.sh.tpl` paths in
`aspect_rules_js`, and [Node.js module resolution and caching](https://nodejs.org/api/modules.html#all-together).
