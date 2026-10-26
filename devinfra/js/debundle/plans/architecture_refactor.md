# Debundle architecture refactor

## Scope and execution

Maintain a repeatable reverse-engineering mapping from unreadable JavaScript to
readable, source-like equivalent modules. The engine processes one program and
one exact spec under explicit runtime and library assumptions. Structural matching
has defined semantics; ambiguity and unresolved claims are diagnostics, never
guesses. Version adaptation is a user workflow. Do not add cross-version
identities, similarity matching, promotion commands, or a content-hash framework.

This ledger covers the cross-phase refactor: source identity and resolution,
semantic facts, assignment and reconstruction planning, validation, emission,
authoring, and their readers. The separate `TODO.md` and trimmed
`ARCHITECTURE_BACKLOG.md` keep unrelated deferred work. Landed behavior belongs
in `SPEC.md`, `docs/`, or its code site; open work stays here. Remove tasks as
they land, and delete this plan when the remaining refactor is complete or
explicitly deferred. Documents outside a plan must not link here.

Use independently reviewable PRs from isolated worktrees. Update every live
reader of a renamed interface, including dependent repositories, without
transitional aliases. Separate mechanical restructuring from semantic changes.

## Current phase vocabulary

| Responsibility                                         | Current representation                             |
| ------------------------------------------------------ | -------------------------------------------------- |
| Parsed program and shared syntax indexes               | `SourceProgram`                                    |
| Binding identity independent of spelling and placement | `BindingId`, retaining chunk scope and SWC hygiene |
| Original statement versus movable syntax fragment      | `StatementId` versus `SourceUnitId`                |
| Reads, writes, effects, and ordering constraints       | `DependencyGraph` over source units                |
| Dense identity for an output module                    | One `ModuleId(pub usize)` handle                   |
| Requested output destinations                          | `ModuleAssignment`                                 |
| Final lowered files and their matching output indexes  | `EmissionFiles`                                    |

`ModuleId` remains the transparent numeric value used on its existing serde
boundary. `ModuleAssignment` is the explicit owner-to-module input. The immutable
validated assignment and the shared reconstruction plan are still open boundaries;
do not imply that current factorization results already provide those guarantees.
An output module quotient may contain cycles.

## Remaining work

### A. Baseline and consumer cutover — in progress

The optimized public large-corpus baseline is measured and recorded in
[`perf/large_web_corpus_baseline.md`](../perf/large_web_corpus_baseline.md).
Baseline measurement is complete; the graph and reader cutover is not. Keep the
registered pipeline inventory, actual report/CLI/Bazel readers, and downstream
controls distinct from the performance result. Stage A is complete only when
each changed interface has an identified reader and its successful, rejected,
or unverified control is recorded. Do not infer consumer coverage from a named
target's transitive build graph.

### B. Identity and assignment — in progress

- PR #12054: require an immutable validated assignment at lowering boundaries.
  It now contains child PR #12061 on that branch only; the combined change has
  not landed on `devel` yet.
- PR #12060: finish the atomic-group identity and consumer cutover. Preserve the
  distinction between bindings, declarations, movable source units, groups that
  must co-locate, and destination modules.

### C. Source preparation and resolution — in progress

Extract reusable structural resolution from lowering and route the remaining
inspection and edit commands through the same resolved facts. A partial or
ambiguous resolution must remain visibly incomplete in every consumer.

### D. Semantic facts and assumptions — in progress

- PR #12059: make analysis assumptions explicit and keep inferred facts distinct
  from author assertions.
- The owner is preparing the canonical catalogue PR; add its identifier after
  it is opened. Keep value flow, callback timing, effects, and dependency
  construction behind explicit inputs/results. Share immutable facts between
  reconstruction and authoring without making cheap inspection pay for
  expensive analysis.

### E. Reconstruction planning

Use one authoritative assignment and a concrete plan for residual placement,
imports, re-exports, readable names, vendor replacement, and loading topology.
Emission must not independently rediscover placement or linkage decisions.

### F. Validation and emission

Validate the concrete plan and require the validated plan at emission boundaries.
Keep AST lowering, final consistency checks, and output writing distinct. Ordinary
runs and edit tools must share acceptance rules, and emitted linkage must match the
validated plan. Retain post-emission safeguards until their construction paths
and equivalent validation are proven; the partial-swap post-strip preconditions
remain documented with that safeguard in `docs/design.md`.

### G. Authoring, CLI, and wire readers

Route proposals, decomposition, refinement, edits, and diagnostics through shared
services. Cut over spec/report vocabulary and all public and downstream consumers
together. Handlers should be thin, assistance deterministic, and edits validated
before writing.

The following serialized or report-facing names still use older factor/cell
vocabulary. They require an explicit reader cutover; internal type renames do not
rename these fields:

- `factorize_proposals` and `factorize_diagnostics` in explain reports and their
  limits sections.
- `edges_to_other_residual_cells`, `other_residual_cells_referenced`, and the
  associated landability-note text. The proposer works on quotient classes,
  while existing consumers still read cell terminology.
- `UnassignedMode::MiniFactors`, serialized as `mini_factors`; a possible
  replacement is `PerAtomicUnit` / `per_atomic_unit`.

Any wire change must update specs, reports, documentation, and actual consumers
together. Do not make it a side effect of an internal rename or leave a second
vocabulary behind through compatibility shims.

### H. Documentation and closure

Keep current behavior in the spec, design docs, skills, and code sites. This plan
alone tracks unfinished architecture work. Close it only after downstream reader
cutovers are complete and remaining correctness issues have landed or been
explicitly deferred.

## Deferred decisions within this refactor

- `OwnerId`/`OwnerIdx` to `NodeId`, `OwnerGraph*` to `NodeGraph*`, and report/CLI
  `owner:N` to `node:N` remain deferred. “Owner” is a coherent graph-node term;
  revisit only as a wholesale rename across live definitions and readers.
- Anonymous statements currently use owners with no declared bindings and
  materializer-specific ordinals. Consider an explicit owner kind only after its
  identity and assignment invariants are pinned alongside PR #12060.
- `chunk` and `artifact` span several phases. Use phase-qualified names for new
  boundary types, but do not mass-rename existing report fields or published
  artifacts. `materialise` / `materialize` differs in one local pipeline name;
  standardize it when touching that code. `peel`, `factor`, `atom`, `owner`, and
  `quotient` are distinct concepts, not spelling variants.
- Reuse of Tarjan/SCC results is valid only after proving that edge sets and
  isolated-node handling match. The gate, quotient report, and other graph walks
  have different contracts; do not consolidate them based only on the algorithm
  name.
- Keep broad crate-internal visibility and large-file movement out of scope unless
  a phase boundary gives callers a narrower, stable interface. Candidate seams
  include selector authoring/resolution/matching, quotient construction,
  lowering's rename ledger, and artifact indexing. Preserve the existing source
  order, hygiene, and pass-through paths when splitting fact extraction, entry
  lowering orchestration, or graph representations.

Other unrelated deferred integrity and test/spec items remain in
`ARCHITECTURE_BACKLOG.md`.

## Correctness-change ordering

For each correctness change, establish a neutral synthetic reproduction, identify
affected downstream constraints, and compare against the baseline. Classify new
rejections as real spec conflicts, analyzer defects, or unsupported
reconstruction. Land diagnostics and repair-enabling capabilities before stricter
dependent checks.

Allow at most two hours of focused investigation and repair for a downstream
conflict, stopping sooner when a deep redesign becomes clear. If repair exceeds
that budget, defer the correctness change with its reproduction, impact, and
remaining work recorded. Continue independent refactoring. Never weaken
validation to force acceptance, and keep the dependent repository on its working
pin until the candidate engine and repaired specs validate together.

## Validation matrix

| Boundary          | Required evidence                                                               |
| ----------------- | ------------------------------------------------------------------------------- |
| Resolution        | Existing CLI outcomes agree; ambiguous/unmatched claims stay explicit           |
| Source identity   | Repeated declarations, anonymous statements, split declarators, rebinding       |
| Initialization    | Live bindings, TDZ/early `undefined`, callback timing, import order             |
| Replacement       | Vendor ownership, generated re-exports, singleton/loading behavior              |
| Mechanical change | Compare acceptance, diagnostics, and generated output to control                |
| Semantic change   | Reproduction and explained downstream differences                               |
| Downstream        | Registered pipelines; prioritize web reconstruction and browser smoke           |
| Performance       | Optimized runs with identical staged inputs and established profiling tools     |
| Publication       | Hooks, packaged skill archive tests, exact-head CI, paired consumer/pin cutover |

Keep existing failures and unverified cases visible. Investigate repeatable
performance regressions before landing. Completion requires live phase boundaries,
shared vocabulary in both repositories, retained established downstream successes,
and every remaining correctness issue landed or explicitly deferred.
