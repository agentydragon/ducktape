# Target-local typed ESLint aspect spike

Date: 2026-10-27 (America/Los_Angeles), based on devel `1696fd6d3b`.
Relocated fixtures validated at `f171a5928ee4a127feb4e81c6a69e3bf47ab1f82`;
the real-library and cost sample below was collected at `b111c348ef6d95f252218b4a5f83d6e768f3f9da`.
Subsequent changes update documentation and comments; runtime behavior is unchanged.

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

This extra dependency is temporary spike wiring for ESLint's JsInfo input gathering.
`ts_project` already publishes the compiler's config closure through TsConfigInfo.
A supported way to consume that metadata from lint integration has not yet been verified;
the generated-filename backreference should not become the compilation wrapper's permanent
interface. `ts_library` remains a small compilation-policy wrapper, while lint integration
owns interpreting the compiler context.

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

All five fixtures in `devinfra/lint/x/typed_eslint/fixtures` are ordinary library targets. The three
negative targets are `manual` so wildcard builds do not fail on intentionally bad specimens.
The generated dependency contract is produced by a Bazel write action. The compiler fixtures
sit outside the product frontend globs; the experimental config supplies their TypeScript
parser, and their compiler checks identifiers.

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
bbr build //devinfra/lint/x/typed_eslint/fixtures:good //devinfra/lint/x/typed_eslint/fixtures:worker \
  //devinfra/lint/x/typed_eslint/fixtures:floating //devinfra/lint/x/typed_eslint/fixtures:misused \
  //devinfra/lint/x/typed_eslint/fixtures:non_thenable

# Expected failure: exactly the three negative specimens fail the new promise rules.
bbr build //devinfra/lint/x/typed_eslint/fixtures:good //devinfra/lint/x/typed_eslint/fixtures:worker \
  //devinfra/lint/x/typed_eslint/fixtures:floating //devinfra/lint/x/typed_eslint/fixtures:misused \
  //devinfra/lint/x/typed_eslint/fixtures:non_thenable \
  --//devinfra/lint/x/typed_eslint:enabled --keep_going
```

RBE invocations:
[baseline: pass](https://app.buildbuddy.io/invocation/13c62f37-6b1e-429c-a602-417a69600791),
[typed positive and negative proof](https://app.buildbuddy.io/invocation/06436f36-5e27-4af5-92ba-3b4740582e55).

## Human and machine reporting

A separate [report-capture run](https://app.buildbuddy.io/invocation/edd128c8-c4d9-443e-870a-ccc0692cfdbd)
requested both `rules_lint_human` and `rules_lint_machine`. It used
`--@aspect_rules_lint//lint:fail_on_violation=false` only to retain diagnostic files and exit codes;
the enforcement run above used the default fail-on-violation setting.

Downloaded artifacts confirmed, for every fixture in the typed run:

- Typed positive reports: human exit 0, machine exit 0, zero SARIF results.
- Typed negative reports: human exit 1, machine exit 1, exactly one SARIF result for the expected rule.

Reproduce with the five-target command above, adding
`--output_groups=+rules_lint_human,+rules_lint_machine --@aspect_rules_lint//lint:fail_on_violation=false`.
Inspect artifacts with `bbapi artifact list edd128c8-c4d9-443e-870a-ccc0692cfdbd --kind build` and
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

## Observed cost

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
