# Residual extraction planning and application

Status: implementation in progress. The source audit and neutral CLI probes
were completed on 2026-11-15; delivered and remaining work is tracked below.

Given an accepted partial spec, find groups of residual source units that can
move into existing or new modules, then apply the exact reviewed assignment.
Keep current owners fixed by default. Existing-owner reorganization is a separate
scope, justified by additional residual reduction.

This is the design and execution plan for W9 in <../TODO.md>; that file remains
the dispatch queue. Burn down the implementation checklist as work lands. Promote
implemented contracts to `SPEC.md` and the CLI docs, retain regression coverage,
and retire this plan after its exit criteria are met. The audit evidence below
is pinned historical evidence, not a claim about the latest engine.

## Required guarantees

- **Sound proposals.** Every plan labeled graph-accepted passes the shared
  complete assignment checker for its exact source, assumptions and baseline.
  Selector realization, emission assessment and runtime evidence remain explicit.
- **A complete invocation.** At least one supported invocation finds a valid peel
  whenever one exists in the declared move scope. This must cover the whole
  residual and all permitted destinations, including joint multi-atom and
  multi-destination batches; it is not merely completeness over candidates a
  heuristic happened to generate. A no-peel result requires exhaustive search
  or an equally complete proved decision procedure.
- **Honest termination.** Unsupported inputs, execution failures, interruption
  and budget exhaustion cannot produce a no-peel conclusion. A bounded search
  may return an accepted witness or an explicitly incomplete result.
- **Tested discovery and application.** Exhaustive small-spec comparisons and
  property tests cover both finding existing peels and rejecting invalid ones.
  Real CLI regressions and emitted behavior cover realization and application;
  agreement between two implementations of the same heuristic is insufficient.
- **Usable agent guidance.** Each delivered CLI contract includes corresponding
  updates to repository-owned skills, `AGENTS.md`, docs and executable examples.
  Packaged guidance must lead an agent through the supported workflow without
  relying on superseded commands or overstating a result's guarantees.

## Workflow and CLI contract

The target is one plan/check/apply workflow, extending the existing
[W9 task][w9]. The implemented fixed-owner contract is documented in
[complete residual search](../docs/module_search.md): exact batch checking,
snapshot-bound plans, exact application and exhaustive discovery. The broader
strategy portfolio and owner reorganization below remain design requirements;
they are not implemented options. Historical audit commands retain their original
meaning and do not describe the new complete mode.

### Operations and guarantees

| Operation                                                          | Responsibility and guarantee                                                                                                                                                                                                      |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `spec validate`                                                    | Resolve selectors and validate the spec against the configured source. Report source/spec validity separately from complete assignment acceptance.                                                                                |
| `graph-summary`, `atoms`, `coverage`, `describe`, `inspect-source` | Inspect the current program and assignment. A displayed group carries no extraction guarantee.                                                                                                                                    |
| `modules propose`                                                  | Search for destination-change plans. Every plan labeled graph-accepted passes the complete assignment gate, including global ESM evaluation. Rejected candidates remain diagnostics.                                              |
| `modules check`                                                    | Check an arbitrary explicit batch against the baseline assignment, using the same complete gate as discovery and application. Support residual units, existing claims, anonymous statements and unions of atoms.                  |
| `modules apply`                                                    | Realize the reviewed plan as spec edits, preserve existing selector metadata, and validate the complete resolved assignment before writing. Validation failure writes no spec edits; dry-run performs the same acceptance checks. |
| `run`                                                              | Materialize the exact spec through the normal pipeline. Report emission and its validation separately from planning and runtime testing.                                                                                          |

All operations consume the same prepared source/spec context. Resolve graph-cache
provenance, engine identity, analysis assumptions and package relocation roots
there; callers should not reconstruct a different environment for each command.
Reuse `AssignmentAnalysis` and the existing typed assignment/checking APIs rather
than introducing a second graph model or compatibility format.

### What a plan means

A plan identifies its exact source, engine, assumptions and baseline assignment
`A0`, then lists explicit source-unit destination changes. Omitted units keep
their destinations. An extension names the actual existing owner to check, not a
stand-in fresh module. Structural atoms stay whole; a plan may move a union of
atoms, possibly into several destinations in one batch.

Start from an accepted `A0` with existing selected units' destinations fixed.
Existing modules can still receive residual units. Seed required atoms and owners
directly from that assignment; do not reconstruct them through independently
gated pairwise merges. Existing-owner reorganization is a later, explicit move
scope, independent of the selected search strategy.

Report residual-origin units removed from the residual and any existing units or
owners moved. Include emitted bytes/lines when emission has been assessed. The
objective is useful residual reduction, with source review deciding semantic
ownership and module size. For a reorganization, report the additional residual
gain over the fixed-owner search and the disturbance to existing modules.

### Proposed discovery options

Permitted moves are independent of search strategy. Selecting a different
strategy must never silently allow more existing assignments to change.

| Option                               | Scope                                                                                                 |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `--existing-assignments fixed`       | Default: every extracted unit keeps its owner. Existing modules may receive residual units.           |
| `--existing-assignments merge_only`  | Later scope: existing modules remain indivisible, but may be merged together or with residual groups. |
| `--existing-assignments repartition` | Later scope: existing modules may be split and regrouped while preserving structural atoms.           |
| `--reorganize MODULE`                | Restrict either reorganization mode to named existing modules; other existing assignments stay fixed. |
| `--destinations new\|existing\|both` | Permit new modules, extensions of existing modules, or both. Default: `both`.                         |
| `--into MODULE`                      | Narrow targets to an exact destination, checking its actual identity when it is an existing owner.    |

Keeping existing assignments fixed does not freeze their module membership:
receiving more residual units is allowed. Implement `fixed` first; broader
scopes follow only after demonstrating additional useful extraction.

| `--strategy`         | Candidate generation                                                                                                                     |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `auto`               | Default portfolio: cheap probes, dependency closures, then bounded joint search.                                                         |
| `single_atom`        | Try individual structural atoms, each of which may contain several source units.                                                         |
| `dependency_closure` | Grow candidates using dependency constraints, distinguishing eager, lazy and co-location edges.                                          |
| `joint`              | Search combinations of atoms and destinations, including batches whose individual moves reject.                                          |
| `complete`           | Decide whether any valid peel exists over the full permitted assignment space; return a checked witness or an exhaustive no-peel result. |

Coarsening and rejoining can initially be techniques inside `joint`, without
additional user-facing switches. Every strategy uses the same acceptance check.

Other controls:

- `--focus ID` seeds a search around selected code while permitting necessary
  companions. `--require ID` requires a returned peel to contain the selected
  residual code; complete mode must find such a peel if one exists. A hard
  restriction such as `--only-units FILE` forbids movement outside the set. If
  that set cuts a structural atom, report the conflict and companions instead of
  silently expanding it or splitting the atom.
- `--max-new-modules` and `--max-moved-existing-units` bound fragmentation and
  disturbance. Unit limits apply to whole groups, never by clipping an atom.
- `--time-budget` and `--max-checks` bound search, with the stopping reason in
  the report. Complete mode has no implicit budget; an explicit exhausted budget
  makes its conclusion incomplete unless it has already found a witness.
- `--limit`, `--include-rejected` and `--format json` control presentation.
  Display limits never remove units or dependencies from checking.

Reject contradictory scopes rather than silently ignoring an option. Residual
reduction is the objective; prefer less disturbance and fragmentation among
useful alternatives. Report code size and unit counts rather than reward the
number of tiny modules produced.

### Internal search and validity

The search operates on complete destination assignments over a fixed prepared
program. Atoms constrain which units must stay together; they do not establish
that moving an atom independently will pass.

1. **Prepare once.** Retain the exact graph, source, analysis assumptions,
   structural atoms and accepted `A0`. Keep all dependency kinds and anonymous
   units. A display or candidate-size cap cannot remove graph constraints.
   Preserve required existing owners directly rather than rebuilding them via
   fallible pairwise contractions.
2. **Construct permitted destinations.** In fixed mode, an extracted atom has
   only its current owner as a destination. A residual atom may stay residual,
   join an allowed existing owner or enter a new module. Keep scaffolding and
   other restricted units in their required placement. Reorganization modes
   broaden these destination sets without weakening the checker.
3. **Check a complete candidate.** Overlay a destination-change batch on `A0`.
   Require permitted edits, complete consistent ownership, intact atoms and the
   shared complete assignment gate. Check cross-destination rebinding,
   initialization and side-effect ordering, and global ESM evaluation, including
   the residual left behind. A local check touching only the moved module cannot
   certify acceptance: its move can change evaluation elsewhere.
4. **Generate and expand candidates.** In the heuristic phase, try individual atoms and dependency-based
   groups at plausible destinations, then use rejection witnesses to prioritize
   larger batches or alternative placements. An eager consumer left dependent
   on retained entry code may need companions extracted too. A lazy reference
   can cross a module boundary, so taking every reachable dependency would
   over-consolidate. Cycle and evaluation-order witnesses suggest repairs; a
   rejected module SCC is not a newly proved structural atom. Merging the SCC
   is one candidate, not the sole permitted repair.
5. **Retain rejected search states.** Heuristics may use a bounded queue of
   deduplicated batches and expand promising candidates even when they reject. A valid joint move
   need not be reachable through individually accepted edits. A failed check
   excludes the exact assignment tested; pruning all supersets requires a
   separately proved necessary constraint. Heuristic ranking or a beam limit
   can omit candidates, but must not be reported as an impossibility proof.
6. **Certify the exact result.** Run the full checker before returning a
   graph-accepted plan. After final names and selectors are authored, resolve the
   entire spec, require assignment equality with the plan and recheck before
   writing. Start with shared prepared analysis and straightforward complete
   checks. Add incremental optimizations only with comparison against the full
   checker; touching-only checks do not replace it.

Conceptually, acceptance is:

```text
permitted_changes(A0, A)
  AND complete_ownership(A)
  AND intact_structural_atoms(A)
  AND complete_assignment_gate(graph, A).accepts
```

The acceptance guarantee and the discovery guarantee are separate. The checker
is intended to be sound under the supported semantics and explicit assumptions;
it can conservatively reject a safe placement. Bounded heuristics may miss
placements the checker would accept. Complete mode must decide the full permitted
assignment space, using the exhaustive procedure below or an equally complete
proved replacement. Small examples test that implementation; they do not define
the scope of its completeness guarantee.

### Complete search contract and algorithm

The required invocation, with the common source/spec context supplied, is:

```bash
debundle modules propose \
  --existing-assignments fixed \
  --destinations both \
  --strategy complete
```

For the fixed prepared graph and assumptions, let `P` be all complete
assignments permitted by the requested move scope. A valid peel is an assignment
in `P` that the authoritative checker accepts and that moves at least one
residual-origin body unit into an explicit extracted owner. Renaming the
residual or routing it into another catchall does not count as progress. This
is completeness relative to the analyzer's supported relocation model, not a
claim to decide all behaviorally equivalent JavaScript rewrites.

The existence contract is:

```text
exists A in P: valid(A) AND residual_gain(A) > 0
  => complete search returns a checked witness

complete search returns exhausted_no_peel
  => no such A exists in P
```

Without restrictive scope options, `P` covers the whole residual, all existing
destinations and arbitrary new-module groupings allowed by the model. Do not
silently restrict it to graph neighbors, single atoms, a small number of new
modules or the candidates selected by another strategy. The existence query may
stop at the first witness; enumerating all peels or finding the best one is a
separate objective. With `--require ID`, search the same space with the additional
requirement that the selected residual code is extracted.

Implement a deterministic exhaustive fallback first:

1. Enumerate every permitted destination assignment of the movable structural
   atoms, retaining the fixed assignments and remaining residual. Include unions
   of atoms, several new modules and simultaneous extensions of existing owners.
   Rejected partial or complete assignments cannot exclude their extensions
   without a proved necessary constraint.
2. Make the new-module space finite without losing solutions. At most one
   nonempty fresh module per movable atom is needed. Enumerate the partitions
   and placements into existing or fresh owners. Prove that representatives
   preserve acceptance, accounting for every module-identity or ordering
   distinction used by the checker and emitter. In particular, do not assume
   label symmetry when `ModuleId` tie-breaking can affect ESM evaluation; prove
   invariance or include those finite ordering cases and realize their identities.
3. Check complete assignments through the shared gate and return the first
   accepted assignment with positive gain. Only exhausted enumeration or a
   complete proved decision procedure may return `exhausted_no_peel`.
4. Allow heuristics to find a witness sooner or order branches, but keep a fair
   exhaustive fallback. Memoization, symmetry reduction, constraint propagation
   and branch pruning require arguments that no valid solution is lost. A beam,
   size ceiling or search budget is not a completeness-preserving optimization.

The proof obligations are finite faithful representation, enumeration coverage,
sound pruning and exact acceptance. Record their arguments with the algorithm;
tests alone do not prove universal completeness. Worst-case search may be
exponential. Report progress and permit explicit cancellation rather than
silently downgrading the contract or promising a polynomial running time.

Keep `found`, `exhausted_no_peel`, `incomplete`, unsupported input and execution
failure distinct in machine-readable output and CLI outcomes. Interruption or
budget exhaustion before a witness produces `incomplete`, never an empty result
that looks exhaustive. A result limit of one still permits this existence
guarantee. Filters that intentionally restrict allowed moves must be explicit in
the reported scope; display filters cannot restrict `P`.

This completeness guarantee concerns graph assignments. A found witness may
still need reviewed names and selectors. All supported source units, including
anonymous statements, must be realizable by the common applicator. A realization
failure is reported as such, not converted into `exhausted_no_peel`; a stronger
ready-to-apply query must check realization as part of its witness contract.

### Proof-oriented tests and release gates

Use small neutral JavaScript programs and real specs to build the analysis.
Generate variations with eager and lazy reads, shared declarations, mutable
bindings, effects, anonymous units and accepted partial starting assignments.
Vary permitted destinations and move scopes. Keep at least one bounded exhaustive
fixture matrix and add property tests with reproducible seeds and shrinking.

The reference search must independently enumerate legal source-unit assignments
and validate concrete candidates through the normal pipeline. It must not reuse
the proposer's candidate generator, pruning, closure construction or atom
contractions as its enumeration oracle. In particular, reference enumeration
must not inherit a planner bug that treats a heuristic contraction as a required
atom. Use real parsed/prepared contexts to keep enumeration practical; do not
mock the gate or fabricate dependency-graph artifacts.

Required properties:

- **Discovery completeness.** Whenever reference enumeration finds a valid peel,
  complete mode returns one. When the reference finds none, complete mode ends
  with `exhausted_no_peel`. Check existential agreement, not equality with one
  preferred partition. Cover required-unit queries too.
- **Proposal soundness.** Every accepted result from every strategy satisfies
  scope and atomic coverage and passes normal whole-assignment validation.
  Apply returned plans through the real CLI and check emitted exports and effect
  traces against source execution for the supported generated programs. Calling
  the same helper already used by discovery is insufficient end-to-end evidence.
- **Realization and composition.** Source resolution after application equals
  the exact planned assignment, including unchanged units. Preview and apply
  agree. Recheck combined plans, and preserve valid joint batches whose separate
  moves reject. Include size-greater-than-one atoms and anonymous statements.
- **Honest failure and scope.** Cancellation, budget exhaustion, unsupported
  inputs, stale contexts and engine failures cannot become `exhausted_no_peel`.
  Output truncation must preserve the decision and its scope, including any
  incompleteness. Fixed owners stay fixed; extensions use the actual owner;
  explicit restrictions are respected without silently broadening scope.
- **Optimization equivalence.** Compare optimized complete search with exhaustive
  reference results; compare incremental checks with full checks, including
  remote ESM TDZ effects. An optimization must preserve both soundness and
  completeness, not just the verdict on previously accepted cases.

Preserve P1-P4, the three-unit seed construction case, remote TDZ and a jointly
valid batch with individually rejected moves as real CLI regressions. Shrink
property-test failures to neutral source/spec/query reproducers, retaining an
accepted baseline and reference witness as needed. Commit minimized regressions
so discovered failures remain covered. Keep source/runtime checks non-vacuous:
matching empty outputs do not exercise initialization or effect ordering.

The unified CLI replacement is not done until these tests run in CI, the
complete invocation has its coverage argument and differential evidence, and an
accepted plan is applied and materialized end to end. Do not retire existing
tools based only on happy-path examples or graph-report agreement.

### Agent guidance and documentation migration

Update guidance alongside each implementing change, describing only delivered
behavior as available. Keep future behavior in this plan until it lands; do not
rewrite skills to invoke commands that have not shipped. Prefer shared recipes
with links from role instructions over duplicating command sequences and
acceptance rules across skills.

| Surface                                                               | Required update                                                                                                                                                                                           |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SPEC.md`, `docs/design.md`                                           | State the shared acceptance contract, permitted move scopes, complete existence guarantee, finite-model/coverage argument and limits of the supported semantics.                                          |
| `docs/cli.md`, `docs/peel_proposer.md`                                | Document actual arguments, shared context, plan identities, result states, complete versus heuristic search, budgets and output limits. Keep CLI help and examples consistent.                            |
| `README.md` task chooser, `docs/planning.md`                          | Make extending an accepted partial spec a direct workflow. Explain when to use complete search and how to review a shortlist; replace readiness labels as dispatch authority.                             |
| `docs/spec_editing.md`, `docs/integration.md`                         | Use exact-batch check/apply for residual and existing claims, including anonymous units. Cover selector realization, unchanged-unit checks, combined-plan validation and refreshing context after edits.  |
| `docs/bazel_integration.md`, `skills/shared/workflow.md`              | Carry the same source/spec/engine context through native and Bazel commands, snapshots, handoffs, gates, emission and runtime checks. Keep invocation evidence distinct from plans.                       |
| `AGENTS.md`                                                           | Record the shared-checker invariant, the complete-search contract, prohibition on treating incomplete searches as no-peel proofs, and required behavioral/property tests. Keep procedures in linked docs. |
| Repository-owned `skills/*/SKILL.md` and packaged references/examples | Route each role through the implemented common workflow and remove obsolete assumptions about binding-only application, independent probes and heuristic landability.                                     |

Review role-specific instructions explicitly:

- `debundle_orchestrator` and `debundle_plan_work`: choose scopes and search
  modes, preserve accepted baselines, interpret negative conclusions correctly,
  and use graph/atom inspection as diagnostics rather than an alternate planner.
- `debundle_intake`: select checked plans, ground semantic ownership and naming,
  and preserve exact destinations and joint batches in the handoff. Existing
  `landable_today` rows alone cannot authorize dispatch.
- `debundle_lane_worker` and `debundle_integrator`: realize and apply exact
  plans, preserve metadata, recheck combined changes and validate the resulting
  emission. Neither a worker's old baseline nor independent passes certify the
  integrated assignment.
- `debundle_architect` and `debundle_explain`: retain semantic ownership review
  and use checked incremental extraction for the code under discussion. Update
  `debundle_mint_names` and `debundle_stabilize` wherever shared context or edit
  contracts change; do not broaden those roles into placement authority.

Edit the repository sources under `skills/`; do not patch installed skill copies
as the implementation. Build affected skill packages and test their packaged
references and runnable examples with the matching engine. Extend the existing
`skills/shared/examples` tests, including `isolated_edit_test` and
`contract_examples_test` where affected, rather than maintain an untested prose
recipe beside the real CLI tests. Add a neutral packaged example for the full
propose/review/check/dry-run/apply/run loop, its runtime observation and honest
negative or incomplete outcomes.

Before retiring a command, search CLI callers, docs, examples, role skills and
their packaged references together. Migrate live callers directly and remove
obsolete recipes in the same change. A final migration check must demonstrate
that an agent following the packaged guidance can perform the new workflow from
its declared starting inputs. Historical audit references remain labeled as
historical; they are not instructions to use retired paths.

### Composition and evidence

The normal workflow is:

```text
inspect → propose → review names/selectors → check → apply → run
```

For an agent extending a partial spec, load the accepted baseline and matching
context, request more residual extractions, inspect the actionable shortlist,
choose semantic owners and names, then validate and materialize a useful batch.
The tool owns constraint discovery and checking; the agent supplies source-backed
ownership and naming decisions. Graph and atom inspection are drill-down tools,
not prerequisites for assembling a separate extraction search by hand. After
application, repeat against the newly accepted baseline; old proposals need
rechecking. If a heuristic finds nothing useful, run complete mode or use its
reported scope and stopping reason to choose another strategy or more budget.
An exhaustive no-peel result justifies considering explicit bounded owner
reorganization; a budget stop does not establish that fixed-owner peels are absent.

Manual batches enter at `check`. Discovery and checking use the same acceptance
path. Application must check the reviewed batch, not silently regroup or replan
it. Source, baseline, assumptions, names or selector changes require rechecking;
stale context is an explicit failure. Before writing, prove that the complete
resolved assignment equals the reviewed plan, including unchanged units.

Acceptance does not compose by concatenation. Two plans accepted independently
against `A0` require another check as a combined batch. Conversely, a joint move
may pass even when each constituent move fails alone. Discovery must therefore
consider multi-atom batches rather than treat individually rejected moves as
proof that their unions cannot work.

Keep distinct evidence states: proposed, graph-rejected, graph-accepted,
realization-pending, realization-failed, emission-assessed, applied and
runtime-tested. “Graph accepted; selectors pending” is useful and honest.
“Ready to apply” additionally requires checked final names, selectors and spec
realization. Graph acceptance is a guarantee under the analyzer's supported
semantics and declared assumptions; it does not replace emission assessment or
runtime evidence.

Report the searched candidate family, permitted moves, candidate counts, budget
and stopping condition. Distinguish unsupported input, engine failure,
interruption, budget exhaustion and search exhaustion. An empty heuristic result
does not prove no extraction exists. Bound diagnostic output and expose progress
during long searches. Completeness claims require a defined search family and an
exhaustive procedure for that family.

### Consolidating the existing tools

Single-atom probing, dependency-closure search, decomposition and coarsening or
rejoining should become strategies under `modules propose`, sharing the plan and
checker above. A strategy chooses which candidates to try; it does not supply a
different placement authority or acceptance standard.

`per_atomic_group` and `coarse_atomic_groups` remain output layout policies under
`run`. They can supply discovery candidates but are not alternative authoring
workflows. Fold `modules refine` into generalized application once its metadata
preservation and whole-assignment checks are supported there. Keep convenience
operations such as `modules merge`, `bindings assign`, rename and unassign only
as frontends to the shared plan/check/apply machinery. Commutation-premise
authoring stays separate: it changes assumptions and needs source audit.

Retire duplicate placement paths after the replacement demonstrates their useful
cases. Migrate callers directly rather than adding transitional compatibility
shims.

### Implementation checklist

- [x] **Correct the demonstrated false positive.** Add P1 as a neutral real-CLI
      regression, with the successful uncapped/grouped control. Retain full
      dependency accounting when a target class is excluded by a size or output
      cap. Update affected readiness documentation to match the actual guarantee.
      This narrow repair does not establish that all proposals are safe.
      The CLI regression failed on the unchanged engine in
      [the baseline run](https://app.buildbuddy.io/invocation/55393916-fb3b-46a3-8057-1c7fa9aa0a42).
      The repair passes both module-application and landability suites in
      [the repaired run](https://app.buildbuddy.io/invocation/707c3357-deb6-4ef9-8cfc-3e7ad741dff0),
      including refusal without spec edits and a grouped Node execution control.
- [x] **Establish the common checker and independent reference.** Preserve the
      accepted starting assignment and prepared context, and expose arbitrary
      complete-batch checking. Cover P3's three-unit atom, remote TDZ, joint moves,
      anonymous units and unchanged-unit identity. Build the small-spec reference
      enumerator and settle the faithful finite representation before relying on
      search optimizations. Document the checker and its evidence states as they
      ship, using existing typed APIs rather than a separate graph model.
      Delivered: `modules check` overlays explicit destination batches on the
      accepted baseline, preserves generated residual destinations and checks
      atoms plus the global gate. Real CLI regressions cover intact three-unit
      atoms, joint moves, actual existing owners, anonymous units, scaffolding and
      malformed batches ([passing suites](https://app.buildbuddy.io/invocation/48c96548-aaee-4473-9077-d19cb4267ccd)).
      An independent oracle enumerates 64 raw statement placements, materializes
      each concrete spec and executes accepted output under Node
      ([passing oracle](https://app.buildbuddy.io/invocation/f2455918-665f-4b1d-bcda-151f7938d7c7)).
      The finite representation and coverage argument now live in
      [the stable guide](../docs/module_search.md#finite-representation-and-coverage-argument).
      Real-source CLI cases distinguish accepted asymmetric cycles from global
      ESM rejection. The constructed remote-TDZ quotient regression remains the
      remote counterexample; a real-source remote-TDZ CLI fixture is still an
      additional coverage opportunity, not evidence already obtained.
- [x] **Deliver exact-batch realization and application.** Generalize application
      for residual and existing claims, including anonymous selectors and preserved
      metadata. Require full post-resolution assignment equality, preview/apply
      agreement and no edits on validation failure. Add CLI and runtime regressions
      and update worker/integration guidance and executable examples together.
      Delivered: `modules apply --assignments` and `--plan` share the checker,
      synthesize residual binding/anonymous claims and preserve existing metadata.
      The exact-application suites exercise joint and multi-destination edits,
      generated residual owners, unchanged-unit identity and Node behavior.
      Legacy `modules refine` can still repair a rejected starting assignment;
      its final assignment uses the same full checker.
- [x] **Deliver complete fixed-owner discovery.** Implement the exhaustive
      fallback over all permitted residual assignments, including multi-atom and
      multi-destination batches. Supply the representation and coverage arguments,
      exhaustive small-spec reference comparisons and property tests above. Verify
      positive witnesses, exhaustive negative conclusions and incomplete outcomes.
      Ship the complete-mode contract, CLI examples and planning/intake guidance
      with the command. Application and search are separate changes after the
      common checker; both are required for the end-to-end milestone.
      Delivered: the unbounded complete mode enumerates the full permitted
      assignment space through finite path-order representatives. Explicit budgets
      report `incomplete`. Shrinking properties compare CLI existence against
      independent exhaustive raw-statement materialization, then check, preview,
      apply and execute each witness. CLI scope, freshness, empty existing owners,
      atoms and repair regressions pass in
      [the edge-case run](https://app.buildbuddy.io/invocation/46bdb03a-dea3-40ee-a4d8-64197aad0922).
- [ ] **Prove a useful fixed-owner workflow end to end.** Run the common workflow
      against a real accepted partial spec, apply a returned plan exactly and
      materialize it through the normal pipeline. Check relevant emitted behavior
      and record meaningful residual reduction and disturbance. Exercise the
      packaged agent recipe too. Keep private source and measurements downstream;
      public regressions and examples remain neutral.
      The shipped skill archive's recipe is exercised from a partial fixture:
      it removes three residual source units, keeps authored units fixed and
      regenerates an entry that executes under Node
      ([passing packaged recipe](https://app.buildbuddy.io/invocation/72d935b5-0647-4d15-8b9c-89758ee67a0e)).
      Real downstream acceptance remains a separate release check.
- [ ] **Accelerate and consolidate after the release gates pass.** Profile actual
      search costs, add heuristic ordering and completeness-preserving optimizations,
      and compare them against the complete reference. Migrate live callers and
      all agent guidance, remove duplicate placement implementations and obsolete
      recipes, and verify the affected skill packages and examples in CI. Promote
      delivered contracts to stable docs as each change lands.
- [ ] **Enable useful bounded owner reorganization.** Add merge-only or
      repartition scopes when they demonstrate additional residual extraction.
      Extend the same soundness, completeness, scope tests and guidance to each
      supported mode. Retain fixed-owner behavior as the default and burn down
      this plan as the remaining work closes.

Individual conservative-effect precision remains separate follow-up work.
Complete discovery is a required deliverable, not a deferred open question.

## Additional proposal follow-ups

Keep the command corpus-neutral. Taxonomy and semantic naming are consumer
policy; graph constraints and candidate validity belong to the engine. The
following existing follow-ups remain in scope after the core guarantees:

- **Proposal metadata.** `ModuleProposal` already reports size, source line
  range, ordinal span, graph-edge counts, and `landability_notes` for specific
  manual-handling cases such as residual dependencies and unsupported
  import/export shapes. Still open: source-locality scores, likely naming
  anchors, and broader warnings for graph-valid proposals that look awkward to
  maintain.
- **Large function-internal seams.** Source-unit proposals cannot split a
  giant handler into independent semantic branches. Until a finer-grained model
  exists, report this granularity limit instead of suggesting arbitrary
  source-unit moves.

## Audit evidence: 2026-11-15

The audit distinguishes inspected contracts and tests, fresh neutral CLI probes,
and architectural inferences. Executable observations belong to the pinned
revision below. No current search is assumed complete from an empty result.

### Assessment

The underlying distinction between source dependencies, structural atoms, and
complete destination assignments is appropriate. The shared complete-assignment
gate is useful, and some application commands enforce strong source-identity
checks. The planning surface does not consistently preserve those distinctions.

There is no unified, reliable implementation of the requested residual query.
Several algorithms construct different assignments under different constraints.
Some outputs certify an exact graph assignment; others classify heuristic
groups without checking the proposed extraction. Discovery and application also
accept different sets of operations.

This audit found a reproducible false-positive proposal: excluding an oversized
prerequisite from proposal output also removes its dependency from readiness
accounting. The consumer is then labeled `peelable_now` and `landable_today`,
but applying it fails the complete graph gate with an ESM TDZ. This is an
implementation defect, beyond the broader incompleteness of heuristic search.

An empty list, a list with no useful landable rows, a rejected candidate, and an
interrupted search have different meanings. None of the current searches proves
that no useful residual extraction exists.

### Revisions, scope, and evidence strength

| Item                                 | Identity / scope                                                                                                                                                                            |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Engine used for fresh neutral probes | `9f0d67477c2b15825b4ca1b4ebed690b3ea33395`                                                                                                                                                  |
| Additional implementation inspected  | Ducktape `d22d34a9b11a4549bef6c3f23b5d264eb3241706`                                                                                                                                         |
| Primary audit surface                | Graph inspection; module proposals; decomposition, coarsening and rejoining; residual layout; extraction probes; assignment/application commands; adjacent gate and commutation diagnostics |

The proposer, quotient kernel, decomposition implementation, and CLI files have
no changes between the two inspected engine revisions. Materialization and
analysis have other changes, so executable results below belong to the tested
pin, not to an unexecuted newer engine. This is not a claim about
the latest remote branch, every Debundle feature, or all JavaScript semantics.

Evidence labels used below:

- **Fresh probe:** ran the real pinned CLI during this audit, using neutral JS
  and real tree specs; no graph-report fabrication.

- **Source:** implementation or test body inspected, without claiming that its
  complete Bazel target was rerun during this audit.
- **Inference:** architectural consequence or risk; distinguished from an
  observed bad execution.

Engine source links are pinned to the tested revision. All engine paths below
are relative to `devinfra/js/debundle/`.

### The domain the tooling needs to represent

For one fixed prepared program and analysis contract, let `U` be the source
units, `A0` the accepted complete assignment, and `R0` its residual units.
A candidate is a finite batch of destination changes producing `A1`. Every unit
outside the batch retains its destination. A fresh destination has an explicit
module identity; an extension names an existing destination.

For the first search scope, previously extracted units keep their destinations.
Candidates may move several whole residual atoms together, possibly into several
destinations. A second, explicit scope can move previously extracted units too.
It should expose their cost and demonstrate the extra residual extraction gained.

The relevant concepts are:

| Concept             | Correct interpretation                                                                              | Current alignment                                                                                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Source unit         | Compiler relocation/ownership identity; not necessarily one binding or one whole original statement | Appropriate. The code handles multi-declarators, anonymous effects, and multiple declaration sites. Binding-only interfaces cannot express all units.              |
| Dependency graph    | Dependencies and effects for a fixed program and explicit analysis assumptions                      | Appropriate foundation. Conservative edges must retain their provenance; a graph constraint is not automatically an intrinsic JavaScript limitation.               |
| Structural atom     | Units required to share a destination under that graph/contract                                     | Appropriate co-location constraint. It is neither a semantic source file nor a sufficient extraction candidate.                                                    |
| Complete assignment | Every unit has a destination, including remaining residual code                                     | The gate and `AssignmentAnalysis` model this. The proposer reconstructs a different working assignment rather than starting intact from `A0`.                      |
| Module              | An authored semantic owner, with names, exports, selectors and initialization obligations           | Partially modeled. Algorithmic classes, SCC repair groups and generated buckets are frequently presented as module suggestions despite lacking semantic ownership. |
| Accepted plan       | A precise candidate assignment tied to input identity and validation scope                          | Pieces exist in the gate, edit checks and reports. There is no common reviewed plan consumed by every discovery and apply path.                                    |
| Useful progress     | Less unclaimed code with understandable ownership, accounting for any existing-code movement        | No common planner objective. File count, class size, dependency coupling and residual line count are different measurements.                                       |

The [structural-atom documentation][two-atoms] already explains the essential
distinction correctly. The trouble is principally how planners use the model,
not the existence of separate source-unit and module graphs.

Acceptance is not monotone under arbitrary splitting or merging. If source
execution requires `A` before `B` before `C`, extracting only `B` while retaining
`A` and `C` together can create `residual -> B -> residual`. Extracting a batch
can repair that quotient. ESM import traversal can also change execution order
in an SCC outside the moved destination. Thus neither atomic coverage nor a
check restricted to the moved module establishes complete-candidate acceptance.

### Tool inventory and actual guarantees

Here, **graph-accepted** means the applicable structural, rebinding and two-pass
realizability checks passed on the stated assignment under the retained graph.
It does not mean that selectors have been synthesized, the full candidate has
been assessed for emission, or runtime equivalence has been established without
the documented assumptions.

| Surface                                                                | Operation / implementation                                                                                                                          | What its result establishes                                                                                                              | State and practical limit                                                                                                                                               |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `atoms`                                                                | Reads structural atomic groups; optional residual/readable filters                                                                                  | Group membership in this graph                                                                                                           | Useful inspection. No extraction verdict.                                                                                                                               |
| `coverage`                                                             | Compares authored claims and atoms                                                                                                                  | Coverage and omitted/split claims; optional proposer annotations                                                                         | Useful inspection. Not a search for all accepted moves.                                                                                                                 |
| `graph-summary`                                                        | Counts graph/assignment facts                                                                                                                       | Summary for the loaded inputs; optional proposer counts                                                                                  | Useful inspection. Proposal options run the same proposer, not an independent search.                                                                                   |
| `describe`, `inspect-source`                                           | Resolve IDs, source spans, edges and current ownership                                                                                              | Source-backed inspection, subject to reported limits                                                                                     | Useful for review. No candidate acceptance guarantee. `describe` defaults to bounded sections; incomplete rows must not be treated as a complete closure.               |
| `scc`, `cluster`                                                       | Inspect the existing module quotient and neighbors                                                                                                  | Current SCCs or adjacency                                                                                                                | Useful diagnostics. An import SCC need not be rejected, and neighbors are not an extraction plan.                                                                       |
| `gate list/describe/cut/diagnose`                                      | Read rejection reports and explain retained graph evidence                                                                                          | Evidence about the reported rejected assignment                                                                                          | Useful diagnostics. A cut is not a proven sufficient edit; missing reports do not prove a successful run.                                                               |
| `modules propose`                                                      | Seed contractions, atomic-DAG closure contractions, greedy extensions/merges, then render classes                                                   | Readiness/addressability labels computed from its resulting classes                                                                      | Works on simple controls. No per-row complete-candidate guarantee; false-positive readiness reproduced below.                                                           |
| `modules decompose`                                                    | Refine current homes by seed fragments, or structural atoms with `--atomic`                                                                         | `candidate_is_realizable: true` certifies the complete reported graph assignment, including structural conflicts and cross-rebinds       | Useful checked diagnostic. It can instead return a rejected candidate. It is not an optimizer for residual reduction.                                                   |
| Decomposition `--include-residual`                                     | Assign residual body atoms to proposed modules; keep import/export scaffolding in entry                                                             | Same complete-candidate guarantee when the verdict is true                                                                               | Can expose a possible global partition; also splits named homes in atomic mode. Not a residual-only edit scope.                                                         |
| Decomposition `--coarsen-blocking-sccs` / `--coarsen-atomic-conflicts` | Merge implicated candidate groups and recheck the complete assignment                                                                               | Final true verdict applies to that whole partition                                                                                       | Checked repair heuristic. No minimality, module-cohesion, or extraction-completeness guarantee. Can stop rejected.                                                      |
| Decomposition `--rejoin-original-modules`                              | Starting from an accepted candidate, try whole-home and pair contractions through the complete gate                                                 | Accepted contractions and final assignment preserve graph realizability                                                                  | Stronger validation than greedy proposer contractions. Scope/objective and performance remain problematic; original residual home is eligible too.                      |
| `residual_atomic_groups` report                                        | Inventory from the pipeline's actual assignment, including an ownership subset                                                                      | Exact baseline groups/edges and baseline gate result                                                                                     | Useful inventory. Does not validate any hypothetical extraction.                                                                                                        |
| `residual_extractions` report                                          | Move each wholly residual body atom independently to a fresh destination; reset to baseline for each probe                                          | Each candidate verdict covers that exact complete graph assignment, including Pass 2 and pre-existing conflicts                          | Useful and narrowly honest. No multi-atom search, existing-destination search, selectors or emitted candidate.                                                          |
| `per_atomic_group` policy                                              | Generate residual modules, one per wholly unclaimed body atom                                                                                       | A successful normal pipeline run has checked its actual generated layout                                                                 | Output policy, not a proposal query or permanent semantic ownership. It can reject.                                                                                     |
| `coarse_atomic_groups` policy                                          | Group generated residual atoms by evaluation intervals/reachability signatures and compatible exports; contract eligible generated-only Pass-1 SCCs | Same normal pipeline checks on the resulting actual layout                                                                               | Separate grouping/repair algorithm. It can reject Pass-2 or mixed-owner cycles; not guaranteed to find a passing coarse layout.                                         |
| `modules apply`                                                        | Recompute current proposals, prove selected source sets/selectors, check the post-edit assignment, then write                                       | Default successful write has source/claim checks plus complete graph gate; outcome distinguishes `claims_checked` from fuller assessment | Works for eligible binding-only proposals. Refuses blocked rows, merge rows and every row containing anonymous units. Cannot consume arbitrary decomposition output.    |
| `modules refine`                                                       | Move existing claims by source-unit ID, preserve metadata, resolve the whole post-spec and compare the complete intended assignment                 | Strong identity check plus complete graph gate; no `--no-verify`                                                                         | Good primitive for existing claims. Explicitly refuses residual units and import/export scaffolding.                                                                    |
| `bindings assign/unassign`, `modules merge/delete`                     | User-specified ownership changes, batch checking before writes                                                                                      | With graph verification, post-edit graph checks; some commands permit explicit `--no-verify`                                             | Useful editing primitives, not candidate generators. `bindings assign` directs fresh residual claims to `modules apply`; unassign has grouped-source-match limitations. |
| `spec propose-commutations`                                            | Draft selectors/premises for eligible opaque sequencing pairs and analyze their effect on blocking SCCs                                             | A possible author-reviewed premise and its stated diagnostic effect                                                                      | Changes assumptions rather than discovering an extraction under unchanged assumptions. Does not prove that the premise is true or replace full candidate validation.    |

Sources: [inspection and planning][plan], [CLI commands][modules-cli],
[proposer][propose], [decomposition][decompose], [residual probes][residual-probes],
[residual layout][layout], [edit gate][edit-gate], [apply][apply],
[refine][refine], [mutation outcomes][outcomes], [commutation workflow][commutations].

#### Sharing versus duplication

The tools do not all have independent graph validators. Typed dependency graph
loading and the realizability implementation are shared, and this is valuable.
However, sharing a predicate does not make the caller's candidate or validation
scope identical.

- Inspection commands can invoke the same proposer. Those are multiple views,
  not multiple candidate algorithms.
- Default decomposition and the proposer share seed construction. The proposer
  adds reachability contractions and greedy merging. Atomic decomposition skips
  same-module reconstruction and has its own complete-candidate repair loop.
- Coarse residual layout has a separate grouping algorithm and a separate
  generated-only SCC contraction loop inside materialization.
- Residual extraction reports use the actual `AssignmentAnalysis` and atomic
  move probes; they do not use the proposer's candidate grouping.
- Claim application has stronger source-level obligations than graph-only
  decomposition. `refine` and `apply` expose different allowed edit domains.

There is therefore duplication of assignment construction, search scope, repair
policy and application contracts, despite a shared realizability foundation.

### Fresh executable findings

These experiments used the pinned production CLI, normal JS/spec parsing and
graph generation. They are diagnostic probes, not newly committed Bazel tests.
The essential inputs and results are included here for reproduction.

#### P1: a size cap creates a false-positive extraction proposal

Source:

```js
const anchor = 1;
const dep = 2;
const consumer = dep + 1;
export { anchor, consumer };
```

Only `anchor` is initially claimed in a named module. The inline-residual
baseline passes. Run `modules propose --size-cap-lines 1` on its generated graph.

Observed:

- `dep` appears only in an `exceeds_size_cap` diagnostic: its source span is two
  lines.
- `consumer` is emitted alone with `status: peelable_now`,
  `landable_today: true` and `source_unit_ids: ["unit:2"]`.
- `modules apply --dry-run` for that exact proposal rejects with
  `Pass 2: ESM evaluation TDZ`, involving the new module and residual.
- The independent residual-atom report also rejects the consumer-only move.

The [renderer][proposal-rendering] first filters candidate classes by size,
then constructs its source-unit-to-candidate map only from the survivors.
Residual edge attribution skips edges when either endpoint is absent from that
map. An excluded prerequisite therefore disappears from readiness accounting.
This contradicts even the narrower promise that `landable_today` means no
outgoing residual-class dependency. It directly disproves a general claim that
`peelable_now` rows are graph-realizable extractions.

#### P2: the simple binding-only application path does work

Use the same source with `const dep = 2;` on one line and the default size cap.
The proposer produces the two-binding `{dep, consumer}` group. Applying it to a
new module succeeds in dry-run and write modes. A subsequent normal pipeline
run with a fresh output directory succeeds.

Node 22.22.2 imports of the source and emitted entry both return:

```json
{ "anchor": 1, "consumer": 3 }
```

The apply result accurately reports `gate: claims_checked`,
`needs_full_assessment: true`, and `effect_assessment_unavailable:
analysis_not_supplied`. It does not disguise the graph check as a full candidate
assessment. The successful post-apply pipeline and Node check are additional
evidence, not implied by the proposal label.

#### P3: valid existing atomic ownership is not faithfully seeded

Source:

```js
let first = 0;
let second = 0;
function update() {
  first += 1;
  second += 1;
}
console.log(first, second);
export { update };
```

Claim `first`, `second` and `update` together in one named `home` module.
The normal pipeline succeeds. The three units are one rebinding atom.

The proposer exits successfully but reports two `atomic_group` and two
`spec_module` seed-contraction rejections. Its construction starts with singleton
classes and attempts pairwise contractions; intermediate partial co-location can
retain forbidden cross-module rebinding. The accepted complete starting owner
is not simply retained as input. See [seed construction][seed].

Atomic decomposition with SCC/atomic-conflict coarsening reports an accepted
complete candidate for this fixture. This is evidence that the algorithms behave
differently on the same accepted source/spec pair; it is not evidence that the
proposer preserved the starting assignment or that its diagnostic count measures
defects in that spec.

#### P4: a landable anonymous extension is not accepted by its apply command

Source:

```js
const anchor = 1;
const dep = 2;
const consumer = dep + 1;
console.log(anchor, consumer);
```

Claim only `anchor`. The proposer emits `extend:anchor` containing `dep`,
`consumer` and the anonymous logging statement, with `landable_today: true`.
Applying the exact row to the required existing destination fails before the
gate with `not a graph-ready binding-only claim`.

This is an application capability gap, distinct from P1's false readiness
classification. Addressable anonymous units can be representable in the spec
and in the proposal, while remaining unsupported by `modules apply`.

### Failure and limitation inventory

Severity here is about answering the residual query correctly and usefully.
Not every limitation is a soundness defect in emitted JavaScript.

| ID  | Classification                                    | Finding and consequence                                                                                                                                                                                                                                                                         | Evidence                                                                                       |
| --- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| F01 | High; reproduced bug                              | Size-filtered prerequisite classes vanish from residual dependency accounting, causing false readiness.                                                                                                                                                                                         | P1; [renderer][proposal-rendering]                                                             |
| F02 | High; reproduced construction failure             | Pairwise seed reconstruction can reject required groups from an accepted starting assignment. Seed diagnostics do not establish that the original spec is invalid.                                                                                                                              | P3; [seed][seed]                                                                               |
| F03 | High; validation-scope mismatch                   | General seed/greedy contractions use target-touching checks. A merge can expose a TDZ elsewhere. There is no complete final check of every rendered extraction against the intact starting assignment.                                                                                          | [quotient checks][quotient-checks]; existing remote-TDZ regression described below             |
| F04 | High; missing capability                          | No shared residual planner searches joint multi-atom batches from the accepted assignment with existing owners fixed. Per-atom failure leaves useful combinations unexplored.                                                                                                                   | [residual probe loop][residual-probes]; proposer/decomposition scope                           |
| F05 | High; incompatible problem scopes                 | Switching between proposal, decomposition and residual-layout strategies changes permitted moves. Residual inclusion in atomic decomposition also fragments named owners.                                                                                                                       | [decomposition construction][decompose]; tool inventory                                        |
| F06 | High; discovery/application gap                   | There is no common arbitrary checked source-unit batch spanning residual claims and existing-claim moves. `refine` refuses residuals; `apply` requires eligible recomputed proposal rows; `assign` redirects fresh claims to `apply`.                                                           | [refine][refine], [apply][apply], [binding edits][binding-edits]                               |
| F07 | Medium; reproduced application gap                | An addressable anonymous proposal can be labeled landable while `modules apply` rejects it categorically.                                                                                                                                                                                       | P4                                                                                             |
| F08 | Medium; coarse repair is not minimal repair       | Merging an entire blocking SCC can produce large mixed-origin groups. Acceptance proves that candidate passes, not that every merged unit is necessary or that all units should share a source file.                                                                                            | [coarsening][decompose] (source/inference)                                                     |
| F09 | Medium; wrong rejoining objective for extraction  | Original-home rejoining includes the original residual home. It can consolidate residual fragments too, undoing entry reduction; mixed-origin groups are excluded from pure-home repair. It does not optimize net extracted units or minimal disturbance.                                       | [rejoining][rejoin] (source/inference)                                                         |
| F10 | Medium; layout is exposed as a search alternative | Generated residual routing can shrink entry or reduce file count without creating authored semantic owners. Its generated-only Pass-1 repair does not solve every Pass-2 or mixed-owner failure.                                                                                                | [layout][layout], [layout repair][layout-repair]                                               |
| F11 | Medium; completeness absent                       | Greedy reachability contractions, line caps and unique-neighbor extension rules prune choices. General greedy search excludes residual-to-residual merges after seeding. No optimality or no-solution certificate results.                                                                      | [merge preconditions][merge-rules]                                                             |
| F12 | Medium; metrics obscure the objective             | Proposal status counts include zero-residual-gain merges and scaffolding. `active_claimed_binding_count` counts only direct binding selectors, omitting source-match and relational claims, so it is not a total of resolved claimed bindings.                                                  | [claim loader][claim-loader], [proposer][propose]                                              |
| F13 | Medium; output bounds and progress                | `--limit` bounds proposals and ordinary diagnostics but not seed rejections. Rejoin attempt/pass limits provide no wall-time bound or partial result while a check is running.                                                                                                                  | [report limits][report-limits], [rejoining][rejoin] (source)                                   |
| F14 | Medium; input/workflow mismatch                   | Ownership-subset runs refuse full graph export, while graph-backed planners require graphs. A graph for full ownership cannot stand in for an assignment where omitted claims become residual.                                                                                                  | [subset regression][subset-test]                                                               |
| F15 | Medium; candidate diagnostic lifecycle            | Edit-gate dry runs write/replace rejection artifacts beside the input graph and remove stale artifacts on success. These files represent the latest attempted edit, not necessarily the graph's saved assignment; callers must retain candidate identity. Parallel probes would need isolation. | [edit-gate contract][edit-gate] (source; concurrency risk not reproduced)                      |
| F16 | Medium; contradictory guarantees                  | Design prose promises that `peelable_now` proposals pass the gate and run correctly in Node. CLI docs call the label readiness only. The implementation and P1 support the weaker contract, and P1 violates even its intended dependency census.                                                | [strong design claim][strong-claim], [CLI qualification][readiness]                            |
| F17 | Medium; testing oracle scope                      | Some differential tests compare touching checks with a touching reference. That establishes equivalence at that scope, not complete-candidate acceptance. Small constructed-graph agreement tests do not establish intact-baseline preservation on real source/spec inputs.                     | [differential test][differential], [kernel tests][kernel-tests]                                |
| F18 | Boundary that must remain explicit                | Graph acceptance alone does not prove source-level naming compatibility, selector realization, emission mapping, effect-proof coverage, vendor/replacement/privacy obligations, or unqualified runtime equivalence.                                                                             | [edit outcomes][outcomes], [proof limits][proof-limits], [CLI assessment contract][assessment] |

F18 describes a legitimate layering boundary. It becomes a tooling defect when
an interface claims a stronger guarantee or lacks the next checking/apply step.
Likewise, conservative unknown effects can block a valid runtime split. This
audit does not prove a particular conservative edge spurious. A rejected
assignment does not justify adding purity or commutation assertions without
source proof.

### What the tests do and do not establish

The following test bodies were inspected; their full Bazel targets were not
rerun for this documentation audit. P1–P4 are fresh neutral CLI experiments.

| Existing coverage                                                                                                                       | What it establishes                                                                                                                                | Remaining gap for this task                                                                                                   |
| --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `residual_extractions_test`: middle-of-residual rejection, pre-existing structural conflicts, ownership subsets and selected probes     | Exact single-atom candidate checks and useful rejection evidence agree with normal assignment behavior on those fixtures                           | No arbitrary joint batch or existing-destination discovery                                                                    |
| `module_decomposition_cli_test`: accepted/rejected refinements, SCC coarsening, rejoining, residual scaffolding and source-anchor cases | Complete candidate verdicts; some accepted maps are applied and executed through Node                                                              | Global partition success is not fixed-owner residual extraction; residual maps remain unsupported by `refine`                 |
| `modules_refine_cli_test`: members, grouped matches, anonymous claims, metadata, atom splits and chunk scope                            | Strong existing-claim transfer, unchanged-unit checking and rejection without writes                                                               | Fresh residual claims remain outside its domain                                                                               |
| `modules_apply_cli_test`: synthesis, reviewed selectors, stale selections and source identity                                           | Some binding-only proposal applications survive normal emission/runtime checks                                                                     | P1 false readiness and P4 anonymous application gap are not closed                                                            |
| `peel_factorize_landability_test`: chains, shared prerequisites, lazy consumers, capped blocked rows, rebinding and decorators          | Useful proposer behavior on selected source shapes                                                                                                 | The existing cap test does not cover a prerequisite itself excluded from proposal output                                      |
| `original_home_merge_rejects_remote_tdz` in `quotient_integration_test`                                                                 | It explicitly asserts a local merge check accepts while the full post-assignment gate rejects a remote TDZ; full-gate rejoining refuses that merge | The proposer still uses the weaker general contraction path                                                                   |
| `gate_differential_test::compare_gate_to_reference`                                                                                     | Ladder/general merge decision agrees with `check_realizability_touching`                                                                           | The reference is deliberately touching, not the full gate                                                                     |
| `planner_and_materializer_agree_on_corpus`                                                                                              | Agreement on several constructed graph-report fixtures                                                                                             | Does not prove proposals preserve any accepted real spec or that each individual rendered proposal is a realizable extraction |
| `coarse_atomic_groups_test`, `per_atomic_group_test`                                                                                    | Generated placement/order/export behavior on specific accepted fixtures and retention of the normal rejection gate                                 | No complete search for a passing coarse layout; no semantic ownership guarantee                                               |

Sources: [residual probe tests][probe-tests], [decomposition tests][decompose-tests],
[refine tests][refine-tests], [apply tests][apply-tests], [factorizer tests][factor-tests],
[kernel tests][kernel-tests], [differential tests][differential],
[coarse tests][coarse-tests].

### Does the construction fit the domain?

**At the checking layer, largely yes.** A fixed dependency graph plus a complete
destination assignment is the right object to validate. Structural co-location,
at-init ordering, lazy import cycles and rebinding need distinct treatment.
`AssignmentAnalysis`, complete assignment validation and `refine`'s expected
source-unit map are useful existing foundations. Reuse them.

**At the discovery layer, only partially.** A quotient is a useful representation
of a candidate partition, but reconstructing the current owners by fallible
pairwise contractions does not faithfully represent an already accepted spec.
Required co-location is a hard constraint; a size cap and a preferred merge
sequence are search policy. Refusing a partial contraction while constructing a
required atom mixes these roles. Such a refusal must not silently turn a hard
input grouping into several speculative classes.
Greedily closing dependencies and merging connected classes can suggest useful
sets, but it does not solve extraction from the retained residual. The residual
is itself a destination with scheduling consequences, not merely unassigned
vertices awaiting independent labels.

**At the semantic ownership layer, graph grouping is insufficient.** A structural
atom says what this analyzer requires to move together. An SCC merge says one
way to repair this candidate. Neither tells us the responsibility, public API,
or desirable size of a human-authored module. Source/caller review remains
necessary. The planner should expose constraints and accepted alternatives,
without turning incidental search classes into asserted semantic homes.

**At the application boundary, the domain is fragmented.** Existing claims and
fresh residual claims are two cases of changing a unit's destination, with
different selector work. They need not have incompatible planning contracts.
Likewise, anonymous effects are first-class source units in the graph and spec;
categorically excluding them from the main residual applicator leaves the
relocation model incomplete as a user workflow.

### Reproducing the neutral checks

Use the tested `debundle` binary. For each source above, make a new fixture root
with `snapshot/main.js`, `js-files.txt` containing `main.js`, and
`vendor_marks.yaml` containing `vendor_marks: []`. Create `modules/main/` and
write `anchor.yaml` with:

```yaml
members:
  - selector: { binding: { name: anchor } }
```

For P3 replace that file with `home.yaml` claiming `first`, `second` and `update`.
Use this `spec_config.yaml`:

```yaml
main_chunk_id: main
module_roots:
  main: main
inputs:
  root: snapshot
  js_list_path: js-files.txt
write_js_tree: true
unassigned_mode:
  main: { kind: inline_in_entry }
```

Run the baseline and query using an absolute `audit_case` directory and the
pinned executable in `DEBUNDLE`:

```bash
"$DEBUNDLE" run \
  --tree-config "$audit_case/spec_config.yaml" \
  --tree-modules "$audit_case/modules" \
  --tree-vendor-marks "$audit_case/vendor_marks.yaml" \
  --tree-source-root "$audit_case" --out-root "$audit_case/out" \
  --graphs --full-report residual_extractions

"$DEBUNDLE" modules propose \
  --graph "$audit_case/out/reports/tree/main/dependency_graph.json.gz" \
  --modules "$audit_case/modules" --tree-config "$audit_case/spec_config.yaml" \
  --source-root "$audit_case/snapshot" --format json --size-cap-lines 1
```

Use the default cap for P2–P4. For P1 save this apply batch, retaining the exact
proposal ID/source-unit list printed by that run:

```json
[
  {
    "proposal_id": "auto_partition_0000",
    "source_unit_ids": ["unit:2"],
    "module": "main/helpers/chain",
    "readable_names": { "consumer": "Consumer" }
  }
]
```

Invoke `modules apply --batch <batch-file> --dry-run` with the same graph,
modules, tree config, source root, size cap and JSON format. P1 must reject;
P2's two-binding batch succeeds. For P4 the destination must be `main/anchor`
and the row includes the anonymous unit; the applicator rejects its shape.
Use a fresh output directory for the normal post-apply pipeline run.

[plan]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/plan.rs
[modules-cli]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/cli/modules_commands.rs
[propose]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/propose.rs
[proposal-rendering]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/propose.rs#L560-L650
[seed]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/quotient.rs#L1763-L1830
[merge-rules]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/quotient.rs#L1426-L1478
[quotient-checks]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/quotient.rs#L879-L1000
[decompose]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/decompose.rs
[rejoin]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/decompose.rs#L1237-L1370
[residual-probes]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/lowering/materialize/residual_extractions_report.rs
[layout]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/lowering/materialize/plan_builder.rs#L1550-L1800
[layout-repair]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/lowering/materialize/mod.rs#L630-L740
[edit-gate]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/cli/edit_gate.rs
[apply]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/cli/module_apply.rs
[refine]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/cli/source_unit_refine.rs
[binding-edits]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/cli/binding.rs
[outcomes]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/cli/outcome.rs
[claim-loader]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/spec/spec_modules.rs#L203-L241
[report-limits]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/plan.rs#L457-L486
[readiness]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/docs/cli.md#L1283-L1286
[assessment]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/docs/cli.md#L318-L340
[commutations]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/docs/cli.md#L1780
[two-atoms]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/docs/design.md#L2991-L3035
[strong-claim]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/docs/design.md#L2202-L2211
[proof-limits]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/docs/design.md#L2774-L2808
[w9]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/TODO.md#L533-L564
[probe-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/residual_extractions_test.rs
[decompose-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/module_decomposition_cli_test.rs
[refine-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/modules_refine_cli_test.rs
[apply-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/modules_apply_cli_test.rs
[factor-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/peel_factorize_landability_test.rs
[kernel-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/quotient_integration_test.rs
[differential]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/peel/gate_differential_test.rs#L260-L280
[coarse-tests]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/coarse_atomic_groups_test.rs
[subset-test]: https://github.com/agentydragon/ducktape/blob/9f0d67477c2b15825b4ca1b4ebed690b3ea33395/devinfra/js/debundle/e2e/per_atomic_group_test.rs#L662
