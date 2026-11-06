# Typed ESLint graduation

Keep the experiment in this one `x/` directory until its remaining integration question is
resolved. The intended production layout is:

```text
eslint.config.js                 # Syntax and type-aware ESLint rules

devinfra/
  js/
    ts_library.bzl               # Thin ts_project policy wrapper
    vitest.bzl                   # Compile and run declared specs
  lint/
    BUILD.bazel                  # ESLint executable and dependencies
    linters.bzl                  # Aspect registration and lint integration
    tests/typed_eslint/          # Automated regression fixtures after graduation

agentplane/app/frontend/
  BUILD.bazel                    # Shared config, bundle, test environment
  tsconfig.json                  # Browser and editor settings
  threads/
    BUILD.bazel                  # Libraries and test targets
    client.ts
    thread_view.tsx
    thread_view.test.tsx
  service_worker/
    BUILD.bazel
    tsconfig.json                # Worker environment
    tsconfig.test.json           # Test environment
    sw.ts
    sw.test.ts
```

## Ownership

- `ts_library` owns compilation defaults and the exact program derived from `srcs` and `deps`.
  It remains a small wrapper around `ts_project`. One target can contain a module or a cohesive
  group of files; one file per compiler invocation is not a repository requirement.
- Checked-in tsconfigs own compiler environments and editor settings. Browser, worker, and
  test environments can extend shared settings. Per-target configs remain generated build
  outputs. Both config targets and inline config dictionaries are documented
  [rules_ts patterns](https://github.com/aspect-build/rules_ts/blob/v3.10.1/docs/tsconfig.md).
- Lint integration owns obtaining the compiler context and applying one shared ESLint config.
  Frontend BUILD files declare sources and dependencies once. Remove the duplicated lint
  source filegroups, dependency lists, and whole-program typed lint tests after parity is proven.
- `vitest_test` owns spec registration. A repository-wide orphan-spec check belongs in one CI
  check rather than an opt-in footer in every BUILD file. This is a separate follow-up from
  typed lint adoption; replace `check_vitest_specs()` only after central coverage is verified.

## Remaining integration question

The current spike proves promise-rule enforcement with the stock aspect and no new upstream
patches. Its extra `ts_config` target in `ts_library` is a temporary input adapter: it reads
rules_ts's generated `tsconfig_<target>.json` and feeds a JsInfo wrapper through `deps`.
That generated-filename dependency is not the intended production interface.

The compiler already exposes its config closure through TsConfigInfo. Determine a supported
way for lint integration to consume that information, preserving the exact compiler program.
Prefer existing stock capabilities; the spike has not yet verified a clean replacement for
the adapter. Any necessary lint-specific metadata handling belongs in the lint layer.
Do not replace the filename dependency with another source/config list maintained by callers.

## Execution and acceptance

Bridge design and the coverage audit can proceed independently. The default switch waits
for all checks below.

1. Resolve the config bridge and remove the temporary adapter from the compilation wrapper.
   Verify inherited configs, generated declarations, imported Promise types, TSX, and worker
   and test environments. Check treatment of authored TypeScript in js_library targets.
2. Audit coverage against the existing typed tests: files, rule options, exclusions, and inline
   directives. CI must select affected library targets so their lint aspects actually run;
   building an application alone does not establish transitive lint coverage.
3. Measure cold and incremental cost across that full set, including recursive project-glob
   discovery and human/machine reporting. Use those measurements to choose useful compilation granularity.
   The current small sample is not evidence for a repository-wide performance improvement.
4. Promote the combined rules into `eslint.config.js` and the fixtures into automated regression
   tests under `devinfra/lint/tests/typed_eslint/`. Remove the opt-in flag and launcher select,
   `devinfra/js/typed_eslint.bzl`, `devinfra/js/eslint.typed.config.js`, per-frontend typed lint
   overrides/aggregation filegroups, and this `x/` directory in the completed migration.

The regression checks must accept the positive specimens, reject each negative specimen for
its expected rule, and verify matching human and SARIF results. Keep existing checks enabled
until their replacement coverage and affected-target selection have passed.
