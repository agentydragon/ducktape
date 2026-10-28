# Direct rules_ts connections trial

Stacked on the stock typed ESLint aspect experiment. Only
`agentplane/app/frontend/connections/` changes compilation policy.

## Build graph

- `:connections` compiles the six production modules with a checked-in `tsconfig.json`.
- `:testing` compiles the three specs and their two support modules with `tsconfig.test.json`.
  The spec glob also registers one Vitest target per spec; each runs alone. The visual harness consumes this
  target for `fixture.js`; that dependency now compiles the specs too.
- Both configs are normal `ts_config` targets. Production extends the shared browser config;
  tests extend the local production options and replace its source selection.
- Consumers use the feature targets without aliases. Source imports and emitted module paths
  stay the same. CSS remains a normal `js_library` dependency.

The stock aspect reads `ts_project.tsconfig` through JsInfo. These two projects do not use
`ts_library`, generated config filenames, or the temporary config dependency adapter.
The opt-in ESLint config selects the appropriate explicit local config for each source;
its generated-project discovery remains in use for packages outside this trial.

Checked-in config globs and Bazel `srcs` must agree. This trial uses matching production
exclusions and a separate test program. Broader adoption still needs a decision about
source ownership and orphan-spec enforcement; the existing whole-program lint source
aggregation remains enabled during this experiment.

## Verification

```sh
bbr test //agentplane/app/frontend/connections:caller_grants_test \
  //agentplane/app/frontend/connections:consent_test \
  //agentplane/app/frontend/connections:management_test
bbr build //agentplane/app/frontend:bundle //agentplane/app/frontend:visual_bundle
bbr build //agentplane/app/frontend/connections:connections \
  //agentplane/app/frontend/connections:testing \
  --//devinfra/lint/x/typed_eslint:enabled
```

The baseline at parent `d3f632ef25` passed typed lint and compilation for
`//agentplane/app/frontend/connections:all`:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/b4330a41-5bec-405c-80c5-c4cfe9490852).
This invocation built test binaries but did not run their suites.

The first trial run exposed a parser-selection edge:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/2b25785b-fdcc-465c-94e0-719b7709270e).
Listing production and test configs together made the test lint action parse the production
config first. Its sandbox contains production declarations rather than production sources,
so that config had no matching roots and TypeScript reported `TS18003` before ESLint tried
the test config. Separate source-pattern overrides select production and test programs.
