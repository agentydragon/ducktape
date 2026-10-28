# Direct rules_ts connections trial

Stacked on the stock typed ESLint aspect experiment. Only
`agentplane/app/frontend/connections/` changes compilation policy.

## Build graph

- `:connections` compiles six production modules.
- `:testing` compiles reusable helpers under `testing/`. The visual harness depends on
  this target for its fixture, without compiling the specs.
- `:specs` compiles the three specs. Each has an explicit Vitest target with literal
  arguments and its own spec path.
- Each target lists its authored inputs explicitly. The existing whole-program lint
  source group reuses those lists; no source collection uses `glob` in this BUILD file.
- All three projects use the same `:tsconfig`, which extends the shared browser config
  and enables declaration/JavaScript emit. Their compiler options are identical, so
  separate prod/test configs would only duplicate file selection.

The local config has no `files`, `include`, or `exclude`. It inherits the frontend's
normal source discovery, restricted in each Bazel action to its declared filesystem.
The compiler also receives dependency declarations, so its discovered program roots
can include visible `.d.ts` files beyond the authored `srcs`. This is not an exact
`files = srcs` contract. Imports still resolve through dependency declarations.
TypeScript inherits relative source patterns relative to their originating config;
see the [extends documentation](https://www.typescriptlang.org/tsconfig/extends.html).

The stock aspect reads `ts_project.tsconfig` through JsInfo. None of these projects
uses `ts_library` or its generated-config adapter. One recursive ESLint source-pattern
override selects the shared checked-in config for production, helpers, and specs.
Other packages retain the generated-project discovery from the parent spike.

Separate configs remain appropriate when compiler environments differ, such as DOM
versus WebWorker libraries or genuinely different ambient types. Vitest's runtime
environment is configured separately and does not require different tsc options here.

## Verification

```sh
bbr test //agentplane/app/frontend/connections:caller_grants_test \
  //agentplane/app/frontend/connections:consent_test \
  //agentplane/app/frontend/connections:management_test
bbr build //agentplane/app/frontend:bundle //agentplane/app/frontend:visual_bundle
bbr build //agentplane/app/frontend/connections:connections \
  //agentplane/app/frontend/connections:testing \
  //agentplane/app/frontend/connections:specs \
  --//devinfra/lint/x/typed_eslint:enabled
```

At implementation commit `71e2c539d0652b5cd6102ef6f1754da15947d28f`, the
[shared-config RBE run](https://app.buildbuddy.io/invocation/562ce12c-6267-4384-83d3-30337156b97f)
passed all three compilation targets, all three Vitest suites (executed), the application
bundle, and the visual-harness bundle. Human lint passed and downloaded SARIF reports
were empty for production, helpers, and specs.

The same temporary valid-TypeScript probe used below was rerun with the shared config:
[report capture](https://app.buildbuddy.io/invocation/eff34121-b0bb-45da-9911-6c6372477a77).
Compilation passed; human and SARIF reports contained exactly the three intended promise
rule errors, with exit code `1` in both formats. Build failure was disabled only to retain
the diagnostic artifacts. The probe was restored and is not committed.

## Earlier split-config setup

The baseline at parent `d3f632ef25` passed typed lint and compilation for
`//agentplane/app/frontend/connections:all`:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/b4330a41-5bec-405c-80c5-c4cfe9490852).
This invocation built test binaries but did not run their suites.

The first trial run exposed a parser-selection edge:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/2b25785b-fdcc-465c-94e0-719b7709270e).
Listing production and test configs together made the test lint action parse the production
config first. Its sandbox contains production declarations rather than production sources,
so that config had no matching roots and TypeScript reported `TS18003` before ESLint tried
the test config. That revision used separate source-pattern overrides; the current
shared config removes this selection entirely.

## Earlier split-config evidence

At `3fbe1d76864c10416c169f02f675c9b6da2dff0d`, the
[corrected run](https://app.buildbuddy.io/invocation/6197f57b-56bf-4c10-b386-8f8e665234e3)
passed both direct compilation targets, all three Vitest suites (cached results), the app
bundle, and the visual-harness bundle. Both targets' human lint actions passed, and their
SARIF reports contained no results. Build artifact digests matched the parent for all
11 emitted JavaScript files and their 11 source maps.

`pre-commit run --all-files` also passed. The
[PR CI Bazel invocation](https://app.buildbuddy.io/invocation/66013adf-68ee-5fc3-aa00-e857fcd8f579)
passed its 178-target affected set against the PR merge commit, including the existing
whole-program lint check.

## Earlier split-config cost observations

These measurements describe the earlier two-target, split-config revision, not the
current three-target shared-config layout. These are single RBE samples from different executor processes, not a benchmark or a
repository-wide speedup claim. Compiler dependencies were largely cached. The first
trial's two compiler actions ran; the baseline compilers were cached, so there is no
cold-compiler timing comparison.

For the initial source set, excluding the shared CSS action and machine reports:

| Layout                         | Human typed-lint actions | Total CPU seconds | Largest action peak |
| ------------------------------ | -----------------------: | ----------------: | ------------------: |
| Parent, per module             |                       11 |            189.17 |            1037 MiB |
| Trial, per feature/environment |                        2 |             33.47 |             705 MiB |

Both rows use the passing baseline and corrected-run invocations above. CPU and memory
come from `executedActionMetadata.usageStats`, not build wall time.

An identical temporary comment was appended to `connection_service_client.ts` in each
layout and then restored. This preserves types while changing an emitted JavaScript
module; the builds selected `//agentplane/app/frontend/connections:all` with typed lint.

| Layout |  Executed compiler actions | Compiler CPU seconds | Human typed-lint actions | Lint CPU seconds |
| ------ | -------------------------: | -------------------: | -----------------------: | ---------------: |
| Parent |             1 (one module) |                 3.62 |                        5 |            97.25 |
| Trial  | 1 (six production modules) |                 9.73 |                        2 |            31.95 |

[Parent edit](https://app.buildbuddy.io/invocation/404dc701-f8f1-4d16-8007-2e46569cd6de),
[trial edit](https://app.buildbuddy.io/invocation/e44e8f17-d70c-471e-b90f-f1815594c376).
The trial's CSS lint action also missed cache (12.14 CPU seconds) although the stylesheet
was unchanged; it is excluded from the TypeScript columns. The grouped compiler did more
work for one edit, while fewer lint actions ran. Neither invocation ran tests or bundles.

## Earlier promise-rule proof

A temporary exported async function in `connections/models.ts` ignored a Promise,
assigned an async function to a `() => void` callback, and awaited a number. Its explicit
return annotation satisfied the existing isolated-declaration policy.

[Compilation and ordinary lint passed](https://app.buildbuddy.io/invocation/c2b1789e-c680-4486-a2b2-28b5db98f553).
The [typed report-capture run](https://app.buildbuddy.io/invocation/d96e86b2-e0fa-44be-a8da-7ff80e9ec16e)
produced exactly `no-floating-promises`, `no-misused-promises`, and `await-thenable` errors
at the three probe locations, with no parser errors. Downloaded human and SARIF reports
agreed; both exit-code artifacts contained `1`. This run used
`--@aspect_rules_lint//lint:fail_on_violation=false` to preserve the reports, so the build
itself returned success. Normal validation above used the default enforcement setting.
The probe was restored and is not part of the PR.
