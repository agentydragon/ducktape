# Explicit import order and constructive residual extraction

Status: target contract; implementation is in progress on draft #16801.
This document does not describe shipped CLI behavior. It supersedes the
unrestricted discovery and implicit scheduling target. The superseded placement
plan is removed; useful constraints and lessons live in the maintained design docs.
The algorithm and correctness argument are in <../docs/placement_algorithm.md>.
The grouped chunk-configuration refactor stays deferred as AR9 in <../TODO.md>.

## Required result

- Every materialized chunk declares an `import_order` priority containing each
  emitted nonentry owner exactly once. Entry is implicit. The priority orders
  every module's canonical import targets; execution remains ordinary DFS.
- One shared verifier checks the module universe, exact target sets and finish
  constraints. Existing atom, rebinding, source/graph provenance and semantic
  gates remain. Ordinary validation, prediction and emission never invoke an
  arbitrary schedule-existence solver or silently change the declared order.
- Capture an accepted traversal as its discovery priority and verify replay.
  Migrate public specs and fixtures, then private consumer specs and their
  engine pin together. Preserve currently passing behavior. The independently
  known-broken private canonical spec is not an acceptance control.
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

## Sequence and ownership

1. **Schema and persistence.** Add `import_order: chunk -> [ModulePath]` to flat
   and tree specs, protobuf saved specs/apply projections and configuration
   read/write APIs. Empty priority is valid for an all-entry chunk; absence for
   a materialized chunk is an error. Preserve ordering-independent inspection.
2. **Shared verifier and pipeline.** Build schedules from supplied priorities,
   export discovery priorities, transport them in graph reports and use them
   in assignment analysis, gate, lowering and saved-analysis application. Remove
   ordinary exact-constructor calls. Test that malformed or failed witnesses
   are errors about that witness, not claims of assignment impossibility.
3. **Migration command and consumers.** Capture from accepted legacy outputs,
   update public examples/fixtures and isolated private specs, and stage the
   private engine pin with the schema migration. Recheck every intended passing
   consumer, including translation/runtime controls where available.
4. **Plans and authoring.** Carry the final priority in plans; update check,
   preview, apply, binding moves and module merge/delete through the same
   complete-map verifier and typed configuration writer. Selector realization
   must preserve the planned map before mutation.
5. **Residual construction and CLI consolidation.** Replace the general
   placement worklist with the proved baseline-preserving operation. Retire
   competing factorizer/decomposition and proposal-ID paths and fold refine
   into exact apply. Retain distinct source/atom/graph inspections. Unsupported
   broader queries fail explicitly rather than silently becoming heuristics.
6. **Documentation and acceptance.** Update SPEC, help, docs, AGENTS, owned
   skills, archive contents, executable recipes and consumer wrappers together.
   Publish the implementation and private migration as reviewable PRs with
   current exact-head validation and measured unseeded downstream evidence.

## Verification requirements

- Independent tiny graph/import-list oracles: discovery-priority replay and
  exact verification; no hidden constructor call on a failed witness.
- Independent tiny source/assignment subset oracles: every returned peel is
  valid, and every feasible required seed under the advertised policy is found.
  Enumerating tiny cases belongs only in tests.
- Real CLI proposal/check/preview/apply/Node positive tests; rejected priority,
  stale input, selector ambiguity, required-group and no-write negative tests.
- Cover cycles, gray versus white targets, shared closures, conditional
  promotion, rebinding, anonymous units, empty modules, generated residuals,
  case-sensitive module roots and isolated chunk identities.
- Deterministic graph-work bounds and incremental/from-scratch comparisons;
  source/selector matching and semantic analysis costs reported separately.
- Full Debundle checks, shipped skill/recipe checks, all private passing-spec
  controls, and one fresh unseeded discovery followed by actual application.

## Incremental delivery

Publish independently validated slices against current `devel` as they become
ready. The design contract, concrete-priority verifier and inert schema/typed
persistence can land before the compiler requires the new field. The compiler
requirement must land with its public fixtures and examples; private priorities
can be authored first, with their engine pin updated only after strict replay.
Plans, edits and the residual constructor must each include their affected CLI
contracts and behavioral tests. Do not require unrelated consumer migrations to
wait for one cold analysis run.

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
