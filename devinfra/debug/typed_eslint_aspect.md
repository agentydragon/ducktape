# Target-local typed ESLint aspect spike

Date: 2026-10-27 (America/Los_Angeles). Implementation tested at
`b111c348ef6d95f252218b4a5f83d6e768f3f9da`, based on devel `1696fd6d3b`.
Subsequent changes only update this report and the spike README.

## Finding

No new upstream patch is needed. The existing ESLint aspect can run the three promise rules
against each `ts_library`'s own TypeScript program. No additional source filegroup is necessary for that aspect.
The spike passes dependency declarations, generated declarations, npm types, TSX,
WebWorker libraries, and an explicit test config through normal Bazel library targets.

Keep the existing whole-program lint tests until coverage and cost have been checked
across the complete migration set. This experiment does not enable typed lint globally,
remove `typed_lint_srcs`, change `check_vitest_specs()`, or fix unrelated application code.

## Integration

Pinned rules_lint 2.9.0 already gathers sources, dependency types, and configs through JsInfo.
The repo's `ts_library` macro now wraps its generated `tsconfig_<target>.json` in `ts_config`,
with the shared base config in `deps`, and adds that wrapper to the library dependencies.
The aspect gathers those files through its existing dependency handling. The generated JSON
and its exact `files` list are unchanged. The wrapper references rules_ts 3.10.1's generated
output name; that convention must be checked when upgrading rules_ts.

The standard ESLint binary selects `devinfra/lint/x/typed_eslint/eslint.config.mjs` when
`--//devinfra/lint/x/typed_eslint:enabled` is set. This flag defaults to false.
The custom config extends the ordinary rules and sets:

```js
parserOptions: {
  project: ["**/tsconfig_*.json"],
  projectService: false,
  tsconfigRootDir: process.cwd(),
}
```

The parser discovers configs in the declared bin-tree inputs and selects a program containing
the linted source. Human and machine actions load the same config. There is no special
per-target environment variable, extra aspect, or report-name patch. Both newly added
upstream patches have been deleted; MODULE.bazel and patches/BUILD.bazel match the base.

Discovering the generated projects matters: the generated config names are not `tsconfig.json`,
and the nearest checked-in config can describe a different program. Agentplane's worker
and worker test have different compiler environments; the latter extends `tsconfig.test.json`.
See the upstream [parser options](https://typescript-eslint.io/packages/parser/) and
[typed-linting troubleshooting](https://typescript-eslint.io/troubleshooting/typed-linting/).

## Behavioral proof

All five fixtures in `util/frontend/x/typed_eslint` are ordinary library targets. The three
negative targets are `manual` so wildcard builds do not fail on intentionally bad specimens.
The generated dependency contract is produced by a Bazel write action.

| Target         | What it proves                                                        | Default config | Typed config               |
| -------------- | --------------------------------------------------------------------- | -------------- | -------------------------- |
| `good`         | Await imported Promise and generated Promise declarations             | Pass           | Pass                       |
| `worker`       | Extended config with ServiceWorkerGlobalScope and client APIs         | Pass           | Pass                       |
| `floating`     | Ignore a Promise imported from another library's emitted declarations | Pass           | Fail: no-floating-promises |
| `misused`      | Pass an async callback to a generated declaration expecting void      | Pass           | Fail: no-misused-promises  |
| `non_thenable` | Await a number returned by a generated declaration                    | Pass           | Fail: await-thenable       |

The worker fixture's checked-in `tsconfig.json` has no files and lacks WebWorker types.
Passing therefore exercises discovery of the generated config and its extended-config closure.
The failures above were the intended rule diagnostics, with no parser or unresolved-project errors.

```sh
# All five compile, with the normal default aspects still enabled.
bbr build //util/frontend/x/typed_eslint:good //util/frontend/x/typed_eslint:worker \
  //util/frontend/x/typed_eslint:floating //util/frontend/x/typed_eslint:misused \
  //util/frontend/x/typed_eslint:non_thenable

# Expected failure: exactly the three negative specimens fail the new promise rules.
bbr build //util/frontend/x/typed_eslint:good //util/frontend/x/typed_eslint:worker \
  //util/frontend/x/typed_eslint:floating //util/frontend/x/typed_eslint:misused \
  //util/frontend/x/typed_eslint:non_thenable \
  --//devinfra/lint/x/typed_eslint:enabled --keep_going
```

RBE invocations:
[baseline: pass](https://app.buildbuddy.io/invocation/2cff3232-a62a-4b9c-8fd4-a40d929f2f4c),
[typed positive and negative proof](https://app.buildbuddy.io/invocation/c002bb71-33aa-400b-8616-b866107edfe5).

## Human and machine reporting

A separate [report-capture run](https://app.buildbuddy.io/invocation/b12f5c0b-14f5-4808-b8b6-fd4fbbd885b8)
requested both `rules_lint_human` and `rules_lint_machine`. It used
`--@aspect_rules_lint//lint:fail_on_violation=false` only to retain diagnostic files and exit codes;
the enforcement run above used the default fail-on-violation setting.

Downloaded artifacts confirmed, for every fixture in the typed run:

- Typed positive reports: human exit 0, machine exit 0, zero SARIF results.
- Typed negative reports: human exit 1, machine exit 1, exactly one SARIF result for the expected rule.

Reproduce with the five-target command above, adding
`--output_groups=+rules_lint_human,+rules_lint_machine --@aspect_rules_lint//lint:fail_on_violation=false`.
Inspect artifacts with `bbapi artifact list b12f5c0b-14f5-4808-b8b6-fd4fbbd885b8 --kind build` and
`bbapi artifact cat <invocation> <report-path>`. Reports and exit-code artifacts use the
standard `AspectRulesLintESLint` names.

## Real library compatibility

All ten explicitly named targets passed together on RBE with the custom config:

| Target                                                        | Relevant shape                                              |
| ------------------------------------------------------------- | ----------------------------------------------------------- |
| `//agentplane/app/frontend/threads:client`                    | OpenAPI/protobuf and frontend dependencies                  |
| `//agentplane/app/frontend/service_worker:service_worker_lib` | WebWorker config, isolated declaration emit                 |
| `//agentplane/app/frontend/service_worker:sw_test_lib`        | Explicit test config and Vitest types                       |
| `//util/frontend:markdown`                                    | TSX, React, marked, DOMPurify                               |
| `//props/frontend/src/lib:client`                             | Nested source path and package-specific npm links           |
| `//haku/console/frontend:client`                              | Generated schema, isolated declaration emit                 |
| `//aiquota/frontend:format`                                   | Cross-package library dependency, isolated declaration emit |
| `//airlock/frontend:api`                                      | API library and generated schema                            |
| `//devinfra/claude/session_export/frontend:api`               | Generated schema, isolated declaration emit                 |
| `//finance/plaid/spend/ui:configuration`                      | Generated schema and frontend types                         |

```sh
bbr build //agentplane/app/frontend/threads:client \
  //agentplane/app/frontend/service_worker:service_worker_lib \
  //agentplane/app/frontend/service_worker:sw_test_lib //util/frontend:markdown \
  //props/frontend/src/lib:client //haku/console/frontend:client \
  //aiquota/frontend:format //airlock/frontend:api \
  //devinfra/claude/session_export/frontend:api //finance/plaid/spend/ui:configuration \
  --//devinfra/lint/x/typed_eslint:enabled --keep_going
```

[Real-target invocation: pass](https://app.buildbuddy.io/invocation/8ccc2c81-d5d8-4211-a7ea-a13c5e95d3f2).
These are representative libraries, not full-program coverage or a run of their application tests.

## Cost and adoption gates

The [real-target run](https://app.buildbuddy.io/invocation/8ccc2c81-d5d8-4211-a7ea-a13c5e95d3f2)
executed ten typed human-report actions. BuildBuddy's RBE usage metadata records
7.77–19.45 CPU seconds and 572–849 MiB peak memory per action. These are observations
from one run, not a benchmark or a comparison against the existing whole-program tests.
The build's elapsed time was 40.3 seconds, with compiler/generator cache hits and an analysis
cache reset after changing the build flag. Retrieve the underlying metadata with
`bbapi --json execution 8ccc2c81-d5d8-4211-a7ea-a13c5e95d3f2`.

The stock aspect runs human reporting as a validation action; machine reporting only runs
when `rules_lint_machine` is requested. Requesting both formats starts two ESLint processes
and constructs the TypeScript program twice. The spike now uses a single aspect in both modes;
selecting the custom config does not add a second syntax-lint pass.

Recommended next implementation:

1. Check discovery cost and config-selection parity across the full target set. The parser
   [documents support for project globs](https://typescript-eslint.io/packages/parser/#project)
   and warns about recursive-glob cost. This experiment exercises the repo's `ts_library`
   config chains, not arbitrary raw ts_project configs or project reference graphs.
2. Audit all authored files covered by the current typed tests against library `srcs`, all
   project-specific overrides and exclusions, and inline directive behavior. Exercise every
   affected library with all three rules before deleting the whole-program tests/filegroups.
3. Verify affected-target selection and measure cold and incremental CI cost on that complete
   set. The aspect visits explicitly selected targets; building an application does not by
   itself prove every transitive library was linted. Include isolated typecheck/test libraries.
4. Make the combined config the default and delete the redundant typed tests/filegroups together
   once those gates pass. Define the treatment of authored TypeScript in js_library targets
   without generated configs before expanding beyond the tested ts_library contract.
