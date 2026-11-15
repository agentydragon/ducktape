# Constructive module placement delivery

Status: 331 test targets pass; measured scheduler optimization in progress, 2026-11-15. This plan replaces
the earlier assignment-enumeration architecture. The mathematical model,
algorithms and general coverage arguments live in
[the placement design](../docs/module_placement.md). The pinned historical
inventory remains in [the audit](../debug/2026_11_15_module_tooling_audit.md).
[The CLI guide](../docs/module_search.md) describes the supported workflow.

## Outcome and scope

Given an accepted partial spec, construct a positive residual peel while keeping
authored owners fixed. Whole residual body atoms may stay at their baseline graph
destination, join allowed existing extracted owners, or join permitted fresh
extracted owners. Required atoms must leave the residual. Several atoms and
several owners may be necessary; no intermediate single move has to pass. Entry,
generated residual layout, authored owners and new extracted owners are distinct
concepts. Generated residual filenames may be recomputed and renumbered after
extraction; moving between residual buckets does not count as progress.

The default workflow is `inspect -> propose -> review -> check -> preview/apply
-> run`. A checked placement answers feasibility; semantic module names and
ownership still need review. An empty advisory shortlist never proves no peel.

The complete invocation must return a witness whenever a permitted accepted peel
exists and resources are unlimited. `found` carries an exact certified placement;
`exhausted_no_peel` requires complete construction; budgets return `incomplete`;
model/certification disagreements are errors. Scope restrictions and display
preferences must not silently change these meanings.

## Delivery sequence and status

1. **Durable plan and neutral repros — committed.** Preserve the accepted gate
   while pinning name-sensitive scheduling, incoming consumer companions and
   multiple new owners. Characterize the old policy separately from semantic
   impossibility. Keep the independent raw-spec oracle throughout migration.
2. **Simultaneous prediction — implemented.** `ModuleAssignmentBatch` updates
   the union of incident source-edge contributions against one final mapping.
   Canonical Gate projection is shared. Global schedule/verdict checks include
   remote components; accepted intermediate single-target moves are unnecessary.
   The incremental API retains a fixed module universe. Placement construction
   remaps canonical IDs when fresh paths change that universe.
3. **Concrete schedules — implemented.** First
   retain the old policy as an explicit target-list witness, then construct
   alternatives only when it fails. The gate, translation/effect validation and
   emission share that witness. Exact emitted target checks caught a public
   export-alias label incorrectly treated as a local read; correct the lowering
   collector instead of tolerating a different emitted graph. Authored YAML never
   stores generated schedules. Snapshot-bound plans replay by deterministic
   derivation and verification.
4. **Constraint construction — integrated.** Start at the finest permitted
   partition: fixed owner anchors plus whole movable atoms. Propagate domain,
   entry, mandatory ownership and constraining-cycle equalities. Derive explicit
   necessary contraction clauses for fresh-owner caps and intrinsic import-schedule
   obstructions. All active C SCCs force co-location, including fallback-promoted
   edges, by the final-postorder-rank argument. Include both incoming consumers
   and partners outside an obstructed SCC's forward closure. Resolve only these
   necessary clauses; certify a completed positive witness once.
5. **Proof and independent small-case evidence validated.** The design supplies
   successful-subtree scheduling and partition-coarsening coverage arguments.
   Finite tests support, rather than establish, universal correctness. Compare
   scheduling against raw import-list permutations and construction against raw
   statement placements materialized through the normal CLI and run under Node.
   Include every small required-unit subset and fresh-owner cap, multi-unit and
   anonymous atoms, rebinding, lazy/eager reads, conditional promotion, generated
   residual baselines, fixed owners and rejected intermediate moves.
6. **CLI and packaged workflow — validated.** Make complete construction the
   default `modules propose`; retain explicitly requested `--strategy auto` only
   as advisory semantic inventory. Reuse `modules check` for exact batches and
   snapshot plans, preview without writes, preserve omitted authored claims while
   allowing configured residual layouts to be regenerated,
   and refuse stale/ambiguous realization before writing. Pin default dispatch,
   explicit advisory mode and contradictory option errors in real CLI tests.
7. **Performance and downstream acceptance — measured follow-through in progress.** Run
   chain/fanout work-count regressions, an optimized prepared-query profile and a
   fresh copied-consumer propose/check/preview/apply/materialize workflow. Require
   positive residual gain, no authored-owner disturbance and exact preservation
   of omitted units. Record engine identity and current-run evidence; historical
   timings are not measurements of this implementation. Build affected skill
   archives and execute the packaged recipe on the combined final engine. Publish
   the same PR and verify exact-head CI separately from focused tests.

The first fresh optimized unscoped profile found about 94% of sampled CPU inside
recursive schedule construction, exceeding a ten-second placement budget because
the scheduler did not poll it. This is required work in this project: solve
independent import SCCs locally, add typed cooperative cancellation that returns
`incomplete`, certify the retained schedule without deriving it twice, and rerun
the optimized profile and full consumer workflow on the resulting engine.

## Correctness boundaries

The source graph, structural atoms and `project_endpoints(..., EndpointView::Gate,
...)` remain authoritative. Do not introduce a second dependency-kind
interpretation, late source reanalysis, purity assumptions or a weaker
realizability gate to obtain peels. Structural atoms are not semantic modules or
placement-dependent SCCs.

The exact scheduler visits real import targets, including entry fanout and
phantom imports. It commits successful subtrees, rolls back failed trials and
uses an independent verifier. A module topological rank is not a concrete ESM
schedule. If a nonentry import SCC fails for every possible root, its forward
closure yields a necessary contraction clause; failure at one root does not.

Every allowed target placement coarsens the initial partition. Each forced
contraction is necessary; every branch clause contains a contraction compatible
with any accepted completion. Contractions strictly reduce block count. Together
with exact scheduling and domain/cap checks, this proves coverage and termination.
Fresh owner names are symmetric only because exact alternative scheduling now
removes preferred-order tie-breaking from existential acceptance.

Production does not enumerate arbitrary atom/owner Cartesian products or invoke
a full validator as its branch predicate. Tiny independent test oracles may do
both. The constructor currently rebuilds the canonical quotient from the prepared
source graph after a contraction; it does not yet maintain incremental quotient
buckets throughout its worklist. Optimize that only from measured profiles and
retain differential equivalence tests. There is no polynomial worst-case claim:
necessary disjunctions can still explore many partitions, and failed nested
schedule trials can repeat work.

## Validation and completion checklist

- [x] Commit the full plan and scheduling TODO before implementation.
- [x] Preserve old-policy acceptance in the initial schedule refactor.
- [x] Prove exact schedule construction and fresh-name symmetry in the design.
- [x] Add simultaneous batch/from-scratch verdict differentials.
- [x] Integrate the constructive generator and complete-default CLI.
- [x] Pass raw placement/import permutation oracles and real CLI/Node regressions.
- [x] Bound chain/fanout partition work and final certification counts.
- [ ] Keep scheduler work local to import SCCs and interruptible by placement deadlines.
- [x] Reconcile SPEC, design, CLI docs, AGENTS, skills and shipped recipe.
- [ ] Profile the optimized implementation and complete fresh downstream acceptance.
- [ ] Publish final commits and verify exact-head CI.

Owner reorganization is a subsequent scope extension only if it demonstrably
unblocks useful residual reduction. It is not needed for this fixed-owner
contract. Broad optimization, best possible module boundaries and minimum owner
counts are separate objectives; the complete query guarantees existence of a
permitted positive peel.
