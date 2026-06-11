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

## Visual Regression Testing

Puppeteer-based visual regression testing via Bazel. Each scenario is a separate Bazel test target in the sub-package next to its component.

```bash
# Run all visual tests
bazel test //props/frontend:visual_tests

# Run a single scenario
bazel test //props/frontend/src/components:visual_DefinitionDetail
```

Baselines are stored in `baselines/` directories next to each component (e.g., `src/components/baselines/`, `src/components/stats/baselines/`, `src/pages/baselines/`). Add test scenarios in `tests/harness/harness.ts` and per-scenario test files (e.g., `visual_test_Foo.mjs`) in the appropriate sub-package.

### Updating baselines

After intentional UI changes, update baselines directly via `bazel run`:

```bash
# Update a single baseline
bazel run //props/frontend/src/components:visual_DefinitionDetail -- --update

# Update all baselines
bazel query 'kind("js_test", //props/frontend/...)' | grep visual_ | \
  xargs -I{} bazel run {} -- --update

# Verify tests pass with new baselines
bazel test //props/frontend:visual_tests --nocache_test_results
```

The `--update` flag takes a screenshot and writes it directly to the source tree baseline directory. This requires `bazel run` (not `bazel test`) because it uses `BUILD_WORKSPACE_DIRECTORY` to locate the source tree.

Failed tests also write `*-actual.png` and `*-diff.png` to `TEST_UNDECLARED_OUTPUTS_DIR` for manual inspection.
