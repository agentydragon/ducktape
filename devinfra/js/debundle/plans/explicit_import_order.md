# Explicit import order and constructive residual extraction

Status: active, with required-contract activation tracked in #16801. Strict enforcement,
ordering-aware edits and the polynomial proposer must land together. Retain
exact-head checks through landing. Consumer migration and final landed-engine
acceptance remain unfinished.
The algorithm and correctness argument are in <../docs/placement_algorithm.md>.
The grouped chunk-configuration refactor stays deferred as AR9 in <../TODO.md>.
The authoring workflow for generated atomic-unit modules and their explicit
priorities is separately deferred as AR10 there.

## Current acceptance gaps

- Audit every plan-producing `modules check` path against the executable-plan
  contract. The ordinary explicit-batch regression with duplicate initializer
  shapes demonstrates graph acceptance followed by selector-synthesis failure.
  Route executable plans through the shared writer preflight; retain a clear
  graph-only result for graph/oracle inspection without claiming selector
  realization. Preserve independent graph-validity oracles when tightening
  the CLI contract.
- Verify preparation cost for selected-view corpus proposals. Reuse compatible
  captured program/source and full-base selector preparation across the corpus;
  never rebuild them per inventory seed. Measure source/selector preparation
  separately from constructor queries and selected-plan certification. A fast
  single-chunk loop does not establish practical whole-corpus preparation.
- Private migration and required-engine adoption remain partial. Complete all
  configured specs, including diagnostic configurations, and run their relevant
  controls on the final landed pin. Preparatory replay on the permissive schema
  is not final required-contract acceptance.
- Complete final-priority configuration for each ownership-subset target.
  A canonical priority cannot also describe a different selected owner universe.
  Give those materializations distinct explicit configurations and retain the
  normal semantic-projection and behavior checks; do not filter an invalid
  priority in the engine to make adoption appear green.
  Capture and verify the accepted traversal for each final ownership variant;
  restricting another assignment's priority is only a candidate, even when that
  assignment was accepted.
- The activation and remaining consumer work must still land. Preserve the
  verified propose/check/preview/apply behavior while trimming independently
  merged preparation from the activation diff. Complete the downstream guidance
  audit with its engine-pin migration.
- Land selected-ownership graph support, selector-backed placement materialization
  and the propose/check/apply writer. Keep the complete base tree as the owner
  of semantic premises and selector anchors. The selected-view workflow uses
  chunk-local owner paths and appends one fresh residual owner while preserving
  existing ownership, export names and priority. Preserve the CLI-to-Node
  regressions for omitted selector anchors and necessary premises, retained
  identity, imports, anonymous units, whole atoms, selector ambiguity, stale
  inputs and no-write failures through final landing.
- Add the passing selected consumer's graph/CLI wrapper and run its unseeded
  proposal, check, preview, actual application and behavior controls on the
  final adopted engine. A rejected canonical graph or a graph-only inventory
  cannot establish that consumer's extraction-loop acceptance.

The fresh `fc0a` integration rehearsal discovers a useful 380-unit peel
unseeded in 6.17 seconds, checks it in 0.81 seconds, previews in 6.42 seconds,
applies in 6.36 seconds and rematerializes in 5.05 seconds. Preview changes no
spec files; all seven reviewed service names survive. Entry residual drops from
825 to 445 source units and from 2,119,089 to 165,722 bytes. Cold source
analysis is separate preparation (8m41.44s); the prepared query reuses it. The
formatted spec emits all 76 app files byte-for-byte identically. Node (10.218s),
Chromium (39.948s), and four pytest cases (31.54s) pass in the
[strict-engine rehearsal](https://app.buildbuddy.io/invocation/4649e7e7-a660-4d22-8471-10da0e7ee59e)
(886.451s total Bazel time). This is still rehearsal evidence, not final landed
pin acceptance. Retain these controls through final adoption.

Remove each gap when its acceptance evidence exists; keep enduring contracts and
lessons in `docs/`.

## Required result

- Every materialized chunk declares an `import_order` priority containing each
  emitted nonentry owner exactly once. Entry is implicit. The priority orders
  every module's canonical import targets; execution remains ordinary DFS.
- One shared verifier checks the module universe, exact target sets and finish
  constraints. Existing atom, rebinding, source/graph provenance and semantic
  gates remain. Ordinary validation, prediction and emission never invoke an
  arbitrary schedule-existence solver or silently change the declared order.
- Capture an accepted traversal as its discovery priority and verify replay.
  Migrate all public and downstream authored specs, fixtures and examples, then
  adopt the required-contract engine downstream. Preserve currently passing
  behavior. Known-broken diagnostic specs must migrate too; their exclusion is
  only from passing-runtime acceptance, not from the new format or tooling.
- Finish with one supported contract and no backward compatibility. Remove
  legacy command routes and aliases, implicit ordering/default repair, decoders
  or adapters for the old placement/ordering contract and any transitional fallback.
  Missing priorities must be rejected wherever materialization requires them.
  Migration/capture helpers do not become a supported legacy execution mode;
  tiny independent enumeration remains confined to test oracles.
- A checked plan binds input identity, complete ownership changes and the final
  import priority. Preview, check, apply and materialization consume that same
  decision. Apply validates ownership and order as one decision, stages both updates before
  mutation, writes each file atomically and preserves omitted
  claims and metadata. A proposed executable plan must be realizable by the
  selector writer as well as accepted by the graph verifier.
- Default residual proposals construct one fresh owner, append it to the
  priority, preserve existing ownership and the projected baseline DFS trace,
  and derive joint groups from structural atoms, rebinding/C closure and event
  bounds. Prove soundness and completeness for that exact family. No general
  partition search or scheduler retry follows the constructor. A negative is
  explicitly scoped to this policy; interruption remains inconclusive.
- Inventory can identify which atoms belong to some permitted peel and explain
  required companions/blockers without enumerating all subsets. Plans from one
  snapshot are not assumed independently composable.
- Supported consumer residual routing must remain explicit. Do not silently
  change a consumer's routing or call a whole-residual rename useful extraction
  to make an unsupported policy appear covered.
- Any optional heuristic repair is explicitly requested, named as incomplete,
  and distinguishes a verified witness, proved obstruction and unknown. It is
  not part of ordinary compilation or the complete proposal operation.
- Existing passing private specs materialize normally. Unseeded scans of their
  nonempty residuals produce useful checked groups in seconds to single minutes
  of prepared query work. Measure source/selector preparation separately. Apply
  at least one discovered extraction and run consumer-appropriate behavior
  controls; a known-unit seeded smoke does not establish this requirement.

## Outstanding work in execution order

1. **Verify strict fixtures and recipes with activation.** The remaining
   fixture and recipe changes exercise the required ordering and writer
   contracts. Author exact emitted owner sets and valid cyclic
   priorities; retain sources, ownership claims, rejection controls and runtime
   assertions. Exercise binding moves, source-unit assignments, merge/delete,
   empty/generated owners and no-write failures through the shared verifier.
   Update the isolated-edit recipe to transport reviewed final priorities into
   the receiving configuration while preserving its relocated paths and unrelated
   fields. Retain residual-reduction assertions against the selected plan rather
   than an incidental extraction count. Acceptance: full Debundle and shipped
   recipe checks pass on the final activation head.
2. **Land the coherent activation.** Keep #16801 rebased onto the landed
   foundations. Keep required-priority enforcement, ordering-aware
   editing and the polynomial proposer in one coherent activation change.
   Keep the native stack's incremental review boundaries explicit. Required enforcement must
   cover materialization, graph export, saved-analysis application and edits;
   malformed or failed priorities must not trigger a replacement scheduler.
   Keep local validation, exact-head hosted checks and merge state distinct.
3. **Complete downstream migration and adoption.** Inventory every checked-in
   authored spec and its build/CLI consumers, including baselines, validation
   fixtures and diagnostic specs. Migrate all of them to the required contract;
   an unmigrated spec cannot remain on a legacy path or pin. Capture priorities
   from accepted traversals and verify replay for every intended-passing consumer.
   The known-broken canonical spec must also migrate, while remaining an explicitly
   rejected diagnostic rather than a passing-runtime control. Stage spec and
   engine-pin changes together where needed. Reuse compatible semantic analysis;
   report cold preparation, selector work and prepared query time separately.
   Preserve residual routing, disclose unsupported generated/catchall routes and
   verify available translation and runtime controls. Do not make unrelated
   migrations wait on one cold run.
4. **Verify the final supported surface.** Retain one `propose` → `check` →
   preview/apply workflow and the distinct source/atom/graph inspections. Verify
   removal of competing factorizer/decomposition, proposal-ID and refine paths,
   and ensure ordinary commands cannot call the retired arbitrary scheduler.
   Update SPEC, help, docs, AGENTS, owned skills, archive contents and consumer
   wrappers with executable examples. Acceptance includes default destinations,
   configured chunk-subtree roots, multi-chunk corpus context, scoped negatives,
   stale inputs, selector ambiguity and no-write refusal behavior. Defer wider
   placement families to W10 and the generated-module authoring redesign to AR10.
   Audit the final source, build rules, adapters, docs, skill archives and examples
   for legacy callers and compatibility paths; reject the old contract in CLI
   tests. Keep intentional invalid-input tests explicit. The final state has no
   opt-in enforcement switch, implicit upgrade, compatibility alias or supported
   execution path for unmigrated specs.
   Verify the retired single-atom report has no remaining callers, including
   downstream wrappers. Preserve its useful behavioral and blocker coverage in
   the constructor inventory and checked plans. Source/atom inspection remains
   distinct from proposal generation; it must not expose a competing placement
   guarantee.

## Verification requirements

- Independent tiny graph/import-list oracles: discovery-priority replay and
  exact verification; no hidden constructor call on a failed witness.
- Independent tiny source/assignment subset oracles: every returned peel is
  valid, and every feasible required seed under the advertised policy is found.
  Enumerating tiny cases belongs only in tests.
- Real CLI proposal/check/preview/apply/Node positive tests; rejected priority,
  stale input, selector ambiguity, required-group and no-write negative tests.
- Exercise the default destination with no `--into`, exact chunk-subtree roots,
  and a corpus containing several configured chunks. Retain each chunk's
  verified source/configuration context through selector preflight and plan
  application; logical plan paths must map through the configured physical root.
- Cover cycles, gray versus white targets, shared closures, conditional
  promotion, rebinding, anonymous units, empty modules, generated residuals,
  case-sensitive module roots and isolated chunk identities.
- Deterministic graph-work bounds and incremental/from-scratch comparisons;
  source/selector matching and semantic analysis costs reported separately.
- Full Debundle checks, shipped skill/recipe checks, all private passing-spec
  controls, and one fresh unseeded discovery followed by actual application.

## Incremental delivery

Publish independently validated slices against current `devel` as they become
ready. Keep #16801 limited to activation in the existing native GitHub stack.
Adopt any server-rewritten head before pushing a rebase. Keep one
reviewable commit per PR, preserve working commands between landings and verify
current stack membership, heads and required checks.

| Landing unit                                      | Target                     | Dependencies and acceptance                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Required-order activation, remaining #16801    | Public native stack        | Required-priority enforcement, ordering-aware editing and the constructive proposer ship with their CLI callers, recipes and guidance. Remove production scheduling search. Require the full integrated suite and useful downstream extraction-loop rehearsal.                                                 |
| Parallel: remaining downstream configurations     | Downstream `main`          | Cover every authored spec, baseline, fixture and diagnostic configuration, including separate final priorities for ownership-subset targets. Verify accepted witnesses and preserve routing.                                                                                                                   |
| 2. Strict-engine adoption and remaining adapters  | Downstream `main`          | Requires migrated configurations and merged activation. Move every consumer to that contract and pin, remove legacy adapters/pins, and pass each intended-passing consumer's applicable materialization, translation and behavior controls. Diagnostics use the new contract too.                              |
| 3. Useful discovered extraction                   | Downstream `main`          | Requires the adopted engine. Land a useful group discovered by the default unseeded operation, carrying its checked priority and selectors; verify residual reduction, preserved owners and behavior on the final spec.                                                                                        |
| 4. Hard-cutover audit and close delivery tracking | `devel`, downstream `main` | Verify all specs are migrated, all consumers use the required contract and no legacy execution or compatibility path remains. After all acceptance gates pass on landed revisions, remove this completed plan and dispatch references; retain maintained proofs/contracts and deferred AR9, AR10 and W10 work. |

Configuration migration and public activation can proceed independently. The
native GitHub stack covers the public review layers in this repository. Downstream
pin PRs wait for the upstream merged revision rather than forming a
cross-repository stack. Keep #16801 limited to the remaining activation work.

The polynomial constructor depends on an accepted explicit baseline certificate,
retained DFS events and the ordering-aware assignment API. Its production caller
is the replacement proposal workflow. Extracting only that kernel before
activation would either change the existing command contract or introduce a
dormant layer. Keep the kernel and its independent oracles with activation; the
canonical constraint projection and graph-priority export are the actual
behavior-preserving foundation seams.

The activation boundary must remain coherent: do not require priorities while
ordinary ownership edits, proposals or packaged recipes still produce specs
without the corresponding final order. Split it further only when each landing
preserves working callers and exact verifier/emitter agreement. Plans, edits and
the constructor ship with their affected CLI contracts and behavioral tests.
Prepare the useful extraction rehearsal early to expose practical gaps before
activation; land the final extraction after downstream adoption. Do not require
unrelated consumer migrations to wait for one cold analysis run.

Preparation on the permissive schema is a rollout step only. The delivered state
requires the new contract everywhere and offers no support for the old one. Any
transitional compatibility needed to keep an intermediate landing coherent must
be removed before this effort is complete.

Local tests, exact-head hosted checks, a landed implementation, private pin
adoption and consumer behavior are separate evidence. Partial slices do not
establish project acceptance. Remove completed checklist items as they land.
Update maintained contracts and retain only useful design constraints outside
this plan; git and the linked PRs retain the implementation history.

## Constraints carried into the remaining work

- Arbitrary schedule existence is NP-complete, even with acyclic initialization
  constraints. Require the author-supplied witness and use the shared verifier;
  never describe witness rejection as proof that no valid ordering exists.
- Successful-subtree commitment, SCC decomposition, caching and iterative
  rollback do not make unrestricted scheduling or partition branching
  polynomial. Remove that fallback from supported operations rather than
  treating faster implementation as the missing completeness proof.
- Preserve canonical Gate activation, true entry, generated owners, empty
  owners, rebindings and whole atoms. A diagnostic graph or one-at-a-time move
  cannot decide a simultaneous peel.
- An all-seed constructor must prepare once, summarize closures on the SCC DAG,
  expand one selected group and certify it once. Do not run the full graph gate
  or build transitive closure sets for every seed.
- Keep completeness scoped to the explicitly proved witness family. A no-peel
  answer for that family does not exclude useful peels that reorder modules,
  extend existing owners or start in generated residual owners.
- A targeted one-unit success cannot establish practical discovery. Require a
  fresh unseeded inventory and actual application; prepared graph queries and
  cold analysis have different acceptance measurements.
- Graph-valid assignments can fail selector realization. Preflight before
  emitting an executable plan, preserve omitted claims, and distinguish
  realization failure from proven graph exhaustion.

The enduring proof and rejected reductions are in
<../docs/placement_algorithm.md> and
<../docs/polynomial_esm_scheduling.md>. These constraints remain here only while
they govern outstanding delivery work.
