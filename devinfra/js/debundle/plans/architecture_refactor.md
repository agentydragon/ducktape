# Debundle architecture refactor

## Contract and execution

Maintain a repeatable reverse-engineering mapping from unreadable JavaScript to
readable, source-like equivalent modules. The engine processes one program and one
exact spec under explicit runtime and library assumptions. Structural matching has
defined semantics; ambiguity and unresolved claims are diagnostics, never guesses.
Version adaptation is a user workflow. Do not add cross-version identities,
similarity matching, promotion commands, or a content-hash framework.

Work in independently reviewable PRs from isolated worktrees. Update all live
callers of a renamed interface, including the dependent private repository, without
transitional aliases. Separate mechanical restructuring from semantic changes.

This is the remaining-work ledger. Record in-progress PRs beside their tasks;
remove tasks as their changes land. Durable implemented contracts graduate to
`SPEC.md`, `docs/design.md`, or their code sites. Delete this plan when its work is
finished or explicitly deferred. Other documentation must not depend on this plan.

## Shared vocabulary

| Concept                                                 | Representation                                     |
| ------------------------------------------------------- | -------------------------------------------------- |
| Parsed input and shared syntax indexes                  | `SourceProgram`, containing input chunks           |
| Binding identity independent of spelling and placement  | `BindingId`, retaining chunk scope and SWC hygiene |
| Original statement versus movable syntax fragment       | `StatementId` versus `SourceUnitId`                |
| Reads, writes, effects, and ordering constraints        | `DependencyGraph` over source units                |
| Units proven to require co-location                     | `AtomicGroup`                                      |
| Requested output destinations                           | `ModuleAssignment`                                 |
| Explicit author contracts and runtime assumptions       | `AnalysisAssumptions`                              |
| Files, imports, exports, replacements, evaluation order | `ReconstructionPlan`                               |
| Plan accepted by the shared validator                   | `ValidatedPlan`                                    |

Replace overloaded owner, factorization, and peel vocabulary by responsibility.
Bindings, declarations, source units, atomic groups, and output modules remain
distinct. A module quotient can contain cycles. IDs belong to the current program;
SWC remains the syntax representation. Types live with their owning subsystem.

## Remaining stages

Reviewable slices:

- PR #12054: immutable `ValidatedAssignment` required by lowering; focused existing
  gate/lowering/execution tests and hosted CI passed.
- PR #12058: the single `ModuleId` handle replaces its redundant nested index.
- Source-unit/dependency-graph vocabulary, atomic-group vocabulary, and explicit
  analysis assumptions are in progress in independent branches.

- [ ] **A. Baseline and consumer inventory.** Enumerate public/private pipelines,
      report readers, CLI wrappers, specs, Bazel interfaces, and packaged skills.
      Record success, rejection, and unverified cases separately, with source/spec/tool
      revisions and performance evidence. Done when every changed interface has an
      identified reader and downstream outcomes have a recorded control.
      Public devel control `f7bf0e0d836528c6cbcbc30f5625059bb30f47e8`:
      `//devinfra/js/debundle/...` passed all 174 tests in invocation
      `335cb393-858c-42a3-a527-0ac76d6ea662` (cached results); the executable built in
      `40ed35e6-a7b9-4170-94ed-28dd986b4e72`. The dependent web pipeline and all six
      existing regeneration/browser/runtime controls passed with this source override
      in `ba724e3b-4a6b-42f9-a937-6402ac7fbab2`. Extraction was cached: fresh optimized
      profiling remains outstanding. Other registered captures without current control
      evidence remain unverified; historical rejections are not new regressions.
- [ ] **B. Names and identity.** Adopt one concept per PR. Distinguish binding
      identity, source position, movement unit, and destination. Replace residual
      sentinels and implicit anonymous-unit classification with explicit types. Done
      when definitions and live consumers agree, without aliases or abandoned readers.
      `ModuleAssignment` is now the canonical placement type; the remaining identity
      and graph changes must preserve its distinction from source facts.
- [ ] **C. Source preparation and exact resolution.** Give parsing, structural
      indexes, and resolution clear ownership. `SourceProgram` now owns prepared
      source and indexes separately from mutable `EmissionFiles`. Extract resolution
      from lowering.
      Commands share successful matches and remaining mismatches. Done when commands
      agree and incomplete resolution cannot appear as complete acceptance.
- [ ] **D. Semantic analysis.** Consolidate value flow, callback timing, effects,
      and dependency construction behind explicit inputs/results. Separate inferred
      facts from author assertions and cheap inspection from expensive analysis. Done
      when reconstruction and authoring reuse immutable semantic facts.
- [ ] **E. Assignment and reconstruction planning.** Replace overlapping
      assignment catalogues and planning structures with an authoritative assignment
      and concrete plan covering residual placement, imports, re-exports, readable
      names, vendor replacement, and loading topology. Done when emission does not
      independently rediscover placement or linkage decisions.
- [ ] **F. Validation and emission.** Validate the concrete plan and require
      `ValidatedPlan` at emission boundaries. Separate AST lowering, final consistency
      checks, and output writing. Done when ordinary runs and edit tools share
      acceptance rules and emitted linkage matches the validated plan.
- [ ] **G. Authoring and CLI.** Route proposals, decomposition, refinement, edits,
      and diagnostics through shared services. Cut over spec/report vocabulary and
      both repositories together. Done when handlers are thin, assistance is
      deterministic, and edits validate before writing.
- [ ] **H. Documentation and closure.** Align current-state architecture,
      contracts, CLI docs, and packaged skills. Consolidate overlapping architecture
      and vocabulary backlog entries here, deleting superseded structures as they
      become unused. Done when downstream cutovers are complete and this plan can go.

Stage A establishes controls before behavior changes. B can proceed one independent
concept at a time; C/D establish inputs for E; E precedes F; G moves callers onto
those services. Update documentation with each landed behavior, not only at H.
Organize code by source, spec resolution, analysis, reconstruction, emission,
authoring, and boundary adapters; avoid a generic common module or framework.

## Correctness-change ordering

For each correctness change, establish a neutral synthetic reproduction, identify
affected downstream constraints, and compare against the baseline. Classify new
rejections as real spec conflicts, analyzer defects, or unsupported reconstruction.
Land diagnostics and repair-enabling capabilities before stricter dependent checks.

Allow at most two hours of focused investigation and repair for a downstream
conflict, stopping sooner when a deep redesign becomes clear. If repair exceeds
that budget, defer the correctness change with its reproduction, impact, and
remaining work recorded. Continue independent refactoring. Never weaken validation
to force acceptance, and keep the dependent repository on its working pin until
the candidate engine and repaired specs validate together.

Retain post-emission safeguards until the concrete plan covers their construction
paths and validation demonstrates equivalent protection.

## Validation matrix

| Boundary          | Required evidence                                                                |
| ----------------- | -------------------------------------------------------------------------------- |
| Resolution        | Existing CLI outcomes agree; ambiguous/unmatched claims stay explicit            |
| Source identity   | Repeated declarations, anonymous statements, split declarators, rebinding        |
| Initialization    | Live bindings, TDZ/early `undefined`, callback timing, import order              |
| Replacement       | Vendor ownership, generated re-exports, singleton/loading behavior               |
| Mechanical change | Compare acceptance, diagnostics, and generated output to control                 |
| Semantic change   | Reproduction and explained downstream differences                                |
| Downstream        | Registered pipelines; prioritize web reconstruction and browser smoke            |
| Performance       | Optimized runs with identical staged inputs and established profiling tools      |
| Publication       | Hooks, packaged skill references, and exact-head CI; paired consumer/pin cutover |

Keep existing failures and unverified cases visible. Investigate repeatable
performance regressions before landing. Completion requires live phase boundaries,
shared vocabulary in both repositories, retained established downstream successes,
and every remaining correctness issue landed or explicitly deferred.
