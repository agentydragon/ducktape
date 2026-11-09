# Tooling and CI gotchas

Surprises met while validating debundle changes and authoring specs.

## Reading CI

- A first-failure summary is not the failure inventory. Inspect the full check
  list and job logs before concluding only one target failed.
- A stale `bbr` console tail (stopped after the runner's `git clean`) does not
  mean the build stalled. Query the requested Bazel invocation ID, which is not
  the hosted-runner invocation ID. An `RST_STREAM` that ends the log stream is
  not a test result either; reconnect with `bb view`.
- `bbapi target history --count N` bounds target groups, not entries, and
  fetches the whole history before filtering; bound it with `--since`. Many
  passing rows share cached results and are not independent executions.
- An unexplained budget or timeout failure is not a flake and not a reason to
  raise the budget. `propose_commutations_budget_test` (30 s) failed three
  times before a duplicate parse was removed; the test's total time also
  included about 106 s of fixture construction outside the timed command. Make
  a fixture record each subprocess (`recorded_run`, diagnostics created first)
  so a timeout leaves a `.started.json` without its `.result.json`.
- Cumulative lazy-download time is not the input-fetch stage duration.

## Analysis reuse

- The analysis artifact's identity hashes the compiled `:staged_analysis`
  closure, not the whole `:pipeline`; lowering-only edits keep it, while
  decoder or fact changes invalidate it. A graph-only engine fix still refuses
  an older artifact if it touches that closure. Refresh through the normal
  pipeline instead of bypassing the guard; analysis compatibility does not
  grant saved-graph compatibility.
- `spec project-analysis` must reject what `analyze` would reject:
  a `resolved_member` naming a missing or foreign-provider member was once
  silently dropped by projection and failed only later in analysis.

## Authoring and queries

- Authored module paths are normalized (`Features/View` becomes
  `features/view`); index lookups by the normalized key, and reject two
  authored paths with the same normalized identity.
- Coordinates differ by report: `unit:N` IDs are local to a graph chunk, while
  `describe` module and binding-home paths keep their authored-root prefix.
  Allowlist names are readable member names; `binding_names` are input
  spellings.
- Several disjoint module roots can map to one chunk. Graph queries scope
  module indexes, binding homes and claims to every root of that chunk;
  dropping the scope check alone would let unrelated YAML contaminate
  resolution.
- `modules merge` takes the chunk's module subtree (`--module-subdir`), not
  `--tree-config`.
- A partially claimed atomic group can be completed only by naming its sole
  owner in `coarse_atomic_groups.complete_partial_atomic_groups_for`; a
  catchall pulls unrelated atoms into the owner and can create a transport
  cycle.
- Emitted entry imports are output-relative (`../../provider/entry.js`) and
  need not name any original source chunk; a source-path resolver alone misses
  readable-name rewrites for them.
- Resource URLs (`new URL(..., import.meta.url)`, dynamic imports) resolve
  against emitted destinations, not the relocated entry directory. Fixtures
  that hand-write an emitted file can encode the old wrong coordinates;
  capture the module as a source chunk instead. A direct `Worker` string
  rewrite has only source-shape coverage: the browser document base is not
  the module URL.
- Independent audit removals do not establish joint removability, and a
  names-only edit still needs source validation.
