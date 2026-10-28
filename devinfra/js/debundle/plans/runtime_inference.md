# Runtime inference migration

Move the remaining existing execution-prefix, heap, capture, callback and reentry
inference into shared typed facts and rules. Preserve precision, fallbacks,
emission, gate decisions and report semantics. New loader inference, generalized
recursive summaries and cross-revision reuse are outside this migration.

## Contracts

- Program points identify chunk, source owner, existing analysis phase and event.
  Preserve conservative pre-call writes, call order, post-call initialization,
  nested invocation order and array-slot order.
- Explicit contexts retain existing arguments/captures, constructor mode,
  revision-sensitive memoization and recursion checks; no new context merging.
- State facts are immutable. Replacement/invalidation creates a successor state;
  later facts cannot change an earlier prefix. Share values and unchanged slots;
  avoid whole-heap copies per point and retire unused transition payloads.
- Typed requests discover idempotent producers on the same worklist. A transfer
  settles only after its input, child-request coverage and required results are
  settled. Missing evidence never proves absence. A settled transfer may still
  have unknown values and execution precision losses.
- Preserve invocation/depth/callback-pool bounds and per-owner reset policies.
  Charge semantic expansions once, never for worklist retries. Diagnostic order
  follows logical events rather than scheduling.
- Static judgments cross only audited one-way boundaries with their complete
  dependencies. Assertions cannot establish the source-only premises admitting
  them. Native proofs consume the complete all-chunk census.
- No final whole-executor wrapper or private runtime inference cache is accepted.

## PRs

- [ ] A: own-property certificates on IterativeRules; remove old Inference mode.
- [ ] B: typed module/export callable capabilities and import projection rules.
- [ ] C: state/point/context/request/result model and shared runtime scheduling.
- [ ] D: expressions, calls, captures, memoization and existing bounded fallback.
- [ ] E: heap containment, properties, descriptors, accessors and state-local proofs.
- [ ] F: callbacks, escapes, reentry, read/rebind promotion and fallback closure.
- [ ] G: native/array/constructor/namespace/metadata execution models.
- [ ] H: remove coarse migration adapters and duplicate authority; update docs.

A, B and C are independent; D depends on C, E on A/C, F and G on D/E, H on all.
Use isolated worktrees, independent PRs and native stacks for real dependencies.

## Transfer requests

Source-prefix producers discover named expression, call, heap, callback and model
transfers on the same fact context. A request identifies its source owner and
parent operation plus a child occurrence, carries a settled input state and
explicit argument/capture environments, and produces a settled output state and
typed value/effect result. Occurrence numbers describe execution order, not
opcodes. Rule and fact families use named enums, including scheduler fixtures.

These execution transitions form an acyclic graph: a result can affect its
parent's continuation, never the immutable predecessor supplied to that child.
Current active-recursion and expansion fallbacks remain terminal results; this
migration does not infer recursive call summaries. The graph can therefore be
evaluated eagerly in dependency order on the common context. No pending child is
substituted with an unknown value. Admission counts/revisions advance once in
semantic output state, independent of scheduler passes.

Completed single-consumer requests/results use `FactContext::take_final` to move
their state and retire obsolete payloads while retaining typed proof dependencies.
Growing facts never use this operation. There can be no future producer or reader
of a retired key. Source-prefix payloads retire after the successor consumes them;
reports retain their existing source and typed proof evidence.

A measured prototype instead suspended and replayed each parent while retaining
all intermediate snapshots. The 1,024-callback control hit a 45-second cutoff
(baseline about 0.17 seconds), and the cached-fallback control retained about
5 GiB. A real `perf` recording attributed about 48% of sampled CPU to cloning
binding/owner sets. That prototype is rejected; merely sharing persistent value
sets is insufficient if complete execution states are repeatedly cloned.

## Acceptance

Freeze an optimized baseline at b3697f5ee41de9fbbe1742c196f3ea594d0d10c0;
its debundle tree is 5786ff3f5d7f2be3776a0842f0e107df1ecd91c0. Compare identical
source/spec pairs: gate decisions, emitted JS, structured evidence and Node
behavior. Normalize temporary paths only. Keep behavior changes in separate PRs.

Run focused CLI fixtures per port, analysis library lint/build, the full debundle
Bazel suite and explicit optimized opaque-callback/value-sharing scale controls.
Test missing-premise wakeups, discovered producers, provenance growth, completion,
budget idempotence and old-prefix isolation. Verify exact-head hosted CI.

Three paired optimized runs on the same staged inputs/machine compare median CPU
and peak RSS; repeatable regression above 25% blocks completion. Use real profiles
to resolve regressions. Private corpus controls supplement public neutral fixtures
when available; unavailable controls remain explicitly unverified.
