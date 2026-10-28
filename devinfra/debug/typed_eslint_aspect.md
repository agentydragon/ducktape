# Target-local typed ESLint aspect spike

Date: 2026-10-27 (America/Los_Angeles). Implementation tested at
`0a606a7dc4ce2275b926d3e2a54827cec3fd2061`, based on devel `1696fd6d3b`.
Subsequent changes add this report and remove an inapplicable parameter docstring entry
from the patch; they do not change the tested implementation.

## Finding

The ESLint aspect can run the three existing promise rules against each `ts_library`'s
own TypeScript program. No additional source filegroup is necessary for that aspect.
The spike passes dependency declarations, generated declarations, npm types, TSX,
WebWorker libraries, and an explicit test config through normal Bazel library targets.

Keep the existing whole-program lint tests until coverage and cost have been checked
across the complete migration set. This experiment does not enable typed lint globally,
remove `typed_lint_srcs`, change `check_vitest_specs()`, or fix unrelated application code.

## Integration

Pinned rules_lint 2.9.0 already gathers the library's sources and dependency types through
JsInfo. Its tsconfig handling only gathers JsInfo: rules_ts 3.10.1's generated
`tsconfig_<target>.json` is a plain file target, so that file is missing. Its `extends`
config is also missing from the ESLint action.

The opt-in patch in `patches/aspect_rules_lint_typed_tsconfig.patch`:

1. Adds the generated config file and the `extends` target's JsInfo closure to the action inputs.
2. Exposes that config's bin-relative path as `BAZEL_ESLINT_TSCONFIG` to both human and machine actions.
3. Names experimental reports separately so default and experimental aspects can coexist.

`devinfra/lint/x/typed_eslint` extends the ordinary ESLint configuration with
`no-floating-promises`, `no-misused-promises`, and `await-thenable`. It sets
`parserOptions.project` explicitly and disables `projectService` for these targets.
The ESLint launcher runs from the bin tree, matching the sources, generated config,
extended configs, and dependency declarations.

Explicit project selection matters: the generated config names are not `tsconfig.json`,
and the nearest checked-in config can describe a different program. Agentplane's worker
and worker test have different compiler environments; the latter extends `tsconfig.test.json`.
See the upstream [parser options](https://typescript-eslint.io/packages/parser/) and
[typed-linting troubleshooting](https://typescript-eslint.io/troubleshooting/typed-linting/).

## Behavioral proof

All five fixtures in `util/frontend/x/typed_eslint` are ordinary library targets. The three
negative targets are `manual` so wildcard builds do not fail on intentionally bad specimens.
The generated dependency contract is produced by a Bazel write action.

| Target         | What it proves                                                        | Default aspect | Typed aspect               |
| -------------- | --------------------------------------------------------------------- | -------------- | -------------------------- |
| `good`         | Await imported Promise and generated Promise declarations             | Pass           | Pass                       |
| `worker`       | Extended config with ServiceWorkerGlobalScope and client APIs         | Pass           | Pass                       |
| `floating`     | Ignore a Promise imported from another library's emitted declarations | Pass           | Fail: no-floating-promises |
| `misused`      | Pass an async callback to a generated declaration expecting void      | Pass           | Fail: no-misused-promises  |
| `non_thenable` | Await a number returned by a generated declaration                    | Pass           | Fail: await-thenable       |

The worker fixture's checked-in `tsconfig.json` has no files and lacks WebWorker types.
Passing therefore exercises the explicit generated config and its extended-config closure.
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
  --aspects=//devinfra/lint/x/typed_eslint:aspect.bzl%eslint --keep_going
```

RBE invocations:
[baseline: pass](https://app.buildbuddy.io/invocation/b17b60be-d135-4717-8145-fb319d62aa36),
[typed positive and negative proof](https://app.buildbuddy.io/invocation/3cd0ae0e-6889-4c2c-986c-6b3f1cb33e80).

## Human and machine reporting

A separate [report-capture run](https://app.buildbuddy.io/invocation/21db2a52-41ba-4f83-a2df-65b25a542579)
requested both `rules_lint_human` and `rules_lint_machine`. It used
`--@aspect_rules_lint//lint:fail_on_violation=false` only to retain diagnostic files and exit codes;
the enforcement run above used the default fail-on-violation setting.

Downloaded artifacts confirmed, for every fixture and both aspects:

- Default reports: human exit 0, machine exit 0, zero SARIF results.
- Typed positive reports: human exit 0, machine exit 0, zero SARIF results.
- Typed negative reports: human exit 1, machine exit 1, exactly one SARIF result for the expected rule.

Reproduce with the five-target command above, adding
`--output_groups=+rules_lint_human,+rules_lint_machine --@aspect_rules_lint//lint:fail_on_violation=false`.
Inspect artifacts with `bbapi artifact list 21db2a52-41ba-4f83-a2df-65b25a542579 --kind build` and
`bbapi artifact cat <invocation> <report-path>`. Reports and exit-code artifacts have separate
`AspectRulesLintESLint` and `AspectRulesLintESLintTyped` names.

## Real library compatibility

All ten explicitly named targets passed together on RBE with the experimental aspect:

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
  --aspects=//devinfra/lint/x/typed_eslint:aspect.bzl%eslint --keep_going
```

[Real-target invocation: pass](https://app.buildbuddy.io/invocation/ee16fb58-dbf4-4e84-aa68-83f087f63dba).
These are representative libraries, not full-program coverage or a run of their application tests.

## Cost and adoption gates

The real-target run executed ten typed human-report actions. BuildBuddy's RBE usage metadata
records 9.42–22.59 CPU seconds and 577–694 MiB peak memory per action. These are observations
from one run, not a benchmark or a comparison against the existing whole-program tests.
The build's elapsed time was 35.6 seconds with many unrelated compiler/generator cache hits.
Retrieve the underlying metadata with `bbapi --json execution ee16fb58-dbf4-4e84-aa68-83f087f63dba`.

The stock aspect runs human reporting as a validation action; machine reporting only runs
when `rules_lint_machine` is requested. Requesting both formats starts two ESLint processes
and constructs the TypeScript program twice. A production switch should replace the normal
aspect with the combined typed aspect, avoiding the duplicated normal pass used by this spike.

Recommended next implementation:

1. Add a supported rules_lint hook for target-specific config inputs and parser context, or
   upstream the small extension demonstrated here. The prototype's `extends` JsInfo handling
   is scoped to this repo's `ts_library`; a general rules_ts integration should handle its
   full TsConfigInfo closure, including direct config labels and reference graphs.
2. Audit all authored files covered by the current typed tests against library `srcs`, all
   project-specific overrides and exclusions, and inline directive behavior. Exercise every
   affected library with all three rules before deleting the whole-program tests/filegroups.
3. Verify affected-target selection and measure cold and incremental CI cost on that complete
   set. The aspect visits explicitly selected targets; building an application does not by
   itself prove every transitive library was linted. Include isolated typecheck/test libraries.
4. Switch the single default aspect and delete the redundant typed tests/filegroups together
   once those gates pass. Keep JS libraries without tsconfigs on the existing syntax rules.
