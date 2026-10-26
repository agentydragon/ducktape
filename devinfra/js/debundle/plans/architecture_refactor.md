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
authoring, and their readers. `TODO.md` and the trimmed
`ARCHITECTURE_BACKLOG.md` retain unrelated deferred work. Landed behavior belongs
in `SPEC.md`, `docs/`, or its code site; open work stays here. Remove tasks as
they land, and delete this plan when the remaining refactor is complete or
explicitly deferred. Documents outside a plan must not link here.

Use independently reviewable PRs from isolated worktrees. Update every live
reader of a renamed interface, including dependent repositories, without
transitional aliases. Separate mechanical restructuring from semantic changes.

## Agreed target vocabulary

This table records the agreed architecture vocabulary. The status column
separates landed representations from target boundaries that still need
implementation and reader cutover.

| Responsibility                                              | Agreed target vocabulary                           | Status                                                                |
| ----------------------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------- |
| Parsed program and shared syntax indexes                    | `SourceProgram`                                    | Landed                                                                |
| Binding identity independent of spelling and placement      | `BindingId`, retaining chunk scope and SWC hygiene | Current SWC identity is chunk scoped; named handle remains Stage B    |
| Identity of an original statement                           | `StatementId`                                      | Required Stage B work                                                 |
| Identity of a movable source fragment                       | `SourceUnitId`                                     | Required Stage B work                                                 |
| Reads, writes, effects, and ordering constraints            | `DependencyGraph` over source units                | Target boundary; active reader cutover                                |
| Units proven to require co-location                         | `AtomicGroup`                                      | Target boundary; PR #12060                                            |
| Explicit author contracts and runtime assumptions           | `AnalysisAssumptions`                              | Target boundary; PR #12059                                            |
| Dense identity for an output module                         | `ModuleId(pub usize)`                              | Landed as one handle with transparent numeric serde representation    |
| Requested output destinations                               | `ModuleAssignment`                                 | Landed                                                                |
| Files, imports, exports, replacements, and evaluation order | `ReconstructionPlan`                               | Target boundary                                                       |
| Plan accepted by the shared validator                       | `ValidatedPlan`                                    | Landed; output syntax and replacements are immutable after acceptance |
| Final lowered files and matching output indexes             | `EmissionFiles`                                    | Current representation                                                |

Use each name for its responsibility. Keep binding identity, original statement
identity, movable source-unit identity, atomic groups, and output modules
distinct. A module quotient may contain cycles; use SCC terminology for actual
strongly connected components. IDs belong to the current program, SWC remains
the syntax representation, and types live with their owning subsystem.

## Remaining work

### A. Baseline and consumer cutover — in progress

Enumerate registered public and downstream pipelines, actual report readers,
CLI wrappers, specs, Bazel interfaces, and packaged skills. Record successful,
rejected, and unverified controls separately with source, spec, and tool
revisions. The optimized public large-corpus baseline is recorded in
[`perf/large_web_corpus_baseline.md`](../perf/large_web_corpus_baseline.md).
That performance evidence supplements the acceptance controls below; it does
not replace them. Stage A is complete only when each changed interface has an
identified reader and downstream outcomes have a recorded control.

Public acceptance control at source revision
`f7bf0e0d836528c6cbcbc30f5625059bb30f47e8`: all 174 tests in
`//devinfra/js/debundle/...` passed in invocation
`335cb393-858c-42a3-a527-0ac76d6ea662`; the executable built in invocation
`40ed35e6-a7b9-4170-94ed-28dd986b4e72`. The dependent web pipeline and all six
existing regeneration, browser, and runtime controls passed with a source
override in invocation `ba724e3b-4a6b-42f9-a937-6402ac7fbab2`. Those named
tests consume the source-override extraction through their dependencies. The
extraction producer action was cached in that invocation, so it is output
acceptance evidence rather than a fresh extraction or performance run. Other
registered captures without current controls remain unverified; historical
rejections remain recorded as prior outcomes, not regressions.

### B. Names and identity — in progress

Land one independent concept per PR. Distinguish binding identity, original
statement identity, movable source-unit identity, anonymous-unit classification,
residual placement, atomic grouping, and destination module identity. Replace
residual sentinels and implicit anonymous-unit classification with explicit
types. Active owner-graph cutover maps `OwnerId` to `SourceUnitId`, keeps a
distinct `SourceUnitIndex` report arena, and maps `OwnerGraph` to
`DependencyGraph`; these identities must not be collapsed into `NodeId`.
Definitions and live consumers must agree without aliases or abandoned readers.

- PR #12060: atomic-group identity and consumer cutover remain active.

### C. Source preparation and exact resolution — in progress

Give parsing, structural indexes, and resolution clear ownership. Extract
reusable structural resolution from lowering and route inspection and edit
commands through the same resolved facts. Commands share successful matches
and remaining mismatches. A partial or ambiguous resolution remains visibly
incomplete in every consumer and cannot appear as complete acceptance.

### D. Semantic facts and assumptions — in progress

Consolidate value flow, callback timing, effects, and dependency construction
behind explicit inputs and results. Separate inferred facts from author
assertions and cheap inspection from expensive analysis. Share immutable facts
between reconstruction and authoring without making cheap inspection pay for
expensive analysis.

- PR #12059: make analysis assumptions explicit and keep inferred facts distinct
  from author assertions.

### E. Assignment and reconstruction planning — in progress

Use one authoritative assignment and a concrete plan for residual placement,
imports, re-exports, readable names, vendor replacement, and loading topology.
Emission must not independently rediscover placement or linkage decisions.

- PR #12067: derive syntax placement from the accepted assignment and verify
  its agreement with the canonical binding catalogue and whole-statement claims.

### F. Validation and emission

Validate the concrete plan and require the validated plan at emission boundaries.
Keep AST lowering, final consistency checks, and output writing distinct. Ordinary
runs and edit tools must share acceptance rules, and emitted linkage must match
the validated plan. Retain post-emission safeguards until their construction
paths and equivalent validation are proven; the partial-swap post-strip
preconditions remain documented with that safeguard in `docs/design.md`.

### G. Authoring, CLI, and wire readers

Route proposals, decomposition, refinement, edits, and diagnostics through shared
services. Cut over spec/report vocabulary and all public and downstream consumers
together. Handlers should be thin, assistance deterministic, and edits validated
before writing.

The following serialized or report-facing names still use older factor/cell
vocabulary and need an explicit reader cutover. Internal type renames do not
rename these fields:

- `factorize_proposals` and `factorize_diagnostics` in explain reports and their
  limits sections.
- `edges_to_other_residual_cells`, `other_residual_cells_referenced`, and the
  associated landability-note text. The proposer works on quotient classes;
  existing consumers still read cell terminology.
- `UnassignedMode::MiniFactors`, serialized as `mini_factors`; the agreed target
  is `PerAtomicGroup`, serialized as `per_atomic_group`.

Any wire change updates specs, reports, documentation, and actual consumers
together. Do not make it a side effect of an internal rename or leave a second
vocabulary through compatibility shims.

### H. Documentation and closure

Keep current behavior in the spec, design docs, skills, and code sites. This plan
tracks unfinished architecture work. Close it only after downstream reader
cutovers are complete and remaining correctness issues have landed or been
explicitly deferred.

## Cross-cutting design constraints

- Preserve source order, SWC hygiene, and pass-through paths when splitting fact
  extraction, entry-lowering orchestration, or graph representations.
- Reuse of Tarjan/SCC results is valid after proving that edge sets and isolated
  node handling match. The gate, quotient report, and other graph walks may have
  different contracts.
- Keep broad crate-internal visibility and large-file movement out of scope
  unless a phase boundary gives callers a narrower, stable interface. Candidate
  seams include selector authoring/resolution/matching, quotient construction,
  lowering's rename ledger, and artifact indexing.
- Organize code by source, spec resolution, analysis, reconstruction, emission,
  authoring, and boundary adapters; avoid a generic common module or framework.
- Keep terms such as `peel`, `factor`, `atom`, `owner`, and `quotient` where they
  describe distinct operations or data. Replace overloaded names according to
  responsibility as boundaries are cut over.

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

Retain post-emission safeguards until the concrete plan covers their construction
paths and validation demonstrates equivalent protection.

## Validation matrix

| Boundary          | Required evidence                                                               |
| ----------------- | ------------------------------------------------------------------------------- |
| Resolution        | Existing CLI outcomes agree; ambiguous or unmatched claims stay explicit        |
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
shared vocabulary in both repositories, retained established downstream
successes, and every remaining correctness issue landed or explicitly deferred.
