# Debundle extraction tooling: state and domain-model audit

Date: 2026-11-15. Status: initial source audit and executable probes completed.

This report audits the tools involved in answering: given an accepted spec,
which groups of residual source units can move into an existing or new module?
The initial scope preserves existing module owners. A separately enabled scope
may reorganize existing modules when doing so enables useful residual reduction.

The report distinguishes implementation contracts, inspected test coverage,
neutral CLI observations, and conclusions that have not yet been demonstrated. It does not treat
failure of a search heuristic as proof that no accepted extraction exists.

## Assessment

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

## Revisions, scope, and evidence strength

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

## The domain the tooling needs to represent

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

## Tool inventory and actual guarantees

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

### Sharing versus duplication

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

## Fresh executable findings

These experiments used the pinned production CLI, normal JS/spec parsing and
graph generation. They are diagnostic probes, not newly committed Bazel tests.
The essential inputs and results are included here for reproduction.

### P1: a size cap creates a false-positive extraction proposal

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

### P2: the simple binding-only application path does work

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

### P3: valid existing atomic ownership is not faithfully seeded

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

### P4: a landable anonymous extension is not accepted by its apply command

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

## Failure and limitation inventory

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

## What the tests do and do not establish

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

## Does the construction fit the domain?

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

## Proposed CLI contract and implementation plan

The agreed direction is one plan/check/apply workflow, building on the existing
[W9 task][w9]. The contracts below are targets, not claims about the commands
audited above. In particular, `modules check` is proposed and `modules apply`
needs a broader contract. Exact flags and serialized shapes remain implementation
decisions.

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

### Composition and evidence

The normal workflow is:

```text
inspect → propose → review names/selectors → check → apply → run
```

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

### Implementation order

1. **Correct the demonstrated false positive.** Add P1 as a neutral real-CLI
   regression, with the successful uncapped/grouped control. Retain full
   dependency accounting when a target class is excluded by a size or output
   cap. This narrow repair does not establish that all proposals are safe.
2. **Build the common checked-plan path.** Preserve the accepted starting
   assignment, then support arbitrary batches, the complete checker, spec
   realization and application. Cover P3's three-unit atom, remote TDZ, a joint
   move whose individual moves reject, anonymous units, unchanged-unit identity
   and preview/apply agreement. Use behavioral CLI regressions rather than a
   second implementation of the checker as the test oracle.
3. **Add joint discovery and demonstrate useful residual reduction.** Search
   unions of atoms with existing owners fixed first. Prove that a returned
   accepted plan can be applied exactly, checked as a complete assignment and
   materialized through the normal pipeline, with emitted behavior checked where
   relevant. Then retire duplicate placement implementations and enable bounded
   existing-owner rearrangements that demonstrate additional useful peels.

Broader completeness and individual conservative-effect precision remain open
questions. This plan records the implementation direction without changing the
audited behavior or treating the proposed guarantees as already delivered.

## Reproducing the neutral checks

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
