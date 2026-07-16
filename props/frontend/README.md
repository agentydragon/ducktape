# Props Frontend

Svelte-based web interface for viewing Props evaluation results.

Runs at `http://localhost:5173` when using the dev server.

## Development

```bash
# Start infrastructure (from props/)
cd props && docker compose up -d

# Run frontend + backend dev servers with watch
bazelisk run //props/frontend:dev
```

For standalone commands (rarely needed):

```bash
bazel build //props/frontend:bundle     # Production build
bazel test //props/frontend:visual_tests # All visual regression tests
```

## Visual Render-Health Testing

Puppeteer-based render-health testing via Bazel. Each scenario is a separate Bazel test target in the sub-package next to its component; the test fails on harness/scenario load failures and uncaught page errors, and publishes the rendered PNG for PR visual review (no checked-in baselines — pixel changes are reviewed on the PR's visual-review page, see `devinfra/pr_visuals/README.md`).

```bash
# Run all visual tests
bazel test //props/frontend:visual_tests

# Run a single scenario
bazel test //props/frontend/src/components:visual_DefinitionDetail
```

Add test scenarios in `tests/harness/harness.ts` and per-scenario test files (e.g., `visual_test_Foo.mjs`) in the appropriate sub-package. Rendered `*-actual.png` files land in `TEST_UNDECLARED_OUTPUTS_DIR` for manual inspection.

## Issue overlay colors

Exact color tokens for the snapshot/critique issue overlay (semantics are in <../docs/SPEC.md>; this is the implementation reference):

| Element                   | Color                                   |
| ------------------------- | --------------------------------------- |
| TP occurrence             | Green (`#dcfce7` bg, `#16a34a` border)  |
| FP occurrence             | Red (`#fee2e2` bg, `#dc2626` border)    |
| Critique issue (TP match) | Blue (`#dbeafe` bg, `#2563eb` border)   |
| Critique issue (FP match) | Orange (`#fed7aa` bg, `#ea580c` border) |
| Novel finding             | Gray (`#f3f4f6` bg, `#6b7280` border)   |
