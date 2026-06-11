# Props Frontend

Svelte-based web interface for viewing Props evaluation results.

## Development

```bash
# Start infrastructure (from props/)
cd props && docker compose up -d

# Run frontend + backend dev servers with watch
bazelisk run //props/frontend:dev
```

For standalone commands (rarely needed):

```bash
bazel build //props/frontend:bundle    # Production build
bazel test //props/frontend:visual_test  # Visual regression tests
```

## Visual Regression Testing

Puppeteer-based visual regression testing via Bazel.

```bash
# Run visual tests (via Bazel)
bazel test //props/frontend:visual_test

# Update baselines after intentional UI changes:
# 1. Build the test harness
cd props/frontend && node tests/harness/esbuild.config.mjs tests/harness/dist

# 2. Run with UPDATE_BASELINES=1 to overwrite baselines
UPDATE_BASELINES=1 HARNESS_PATH=tests/harness/dist/harness.js node tests/visual-regression.spec.js

# 3. Verify the new baselines pass
bazel test //props/frontend:visual_test --nocache_test_results
```

Baselines are stored in `tests/visual-regression.spec.ts-snapshots/` (committed to git). Add test scenarios in `tests/harness/harness.ts`.
