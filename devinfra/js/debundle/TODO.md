# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Active work queue

This file is the dispatch queue, not a design record or changelog. Detailed
plans and evidence live here:

- <docs/selector_resolution.md> — how selectors resolve, and the measured reason
  the architecture is shaped that way.
- <plans/relational_selectors.md> — the remaining selector-language work.
- <plans/automated_spec_workflows.md> — automation-first CLI/workflow design,
  including porting a spec to the next bundle version.
- <plans/factor_vocabulary_rename.md> — internal naming and wire-format decisions.
- <plans/module_proposals.md> — proposal metadata and granularity follow-ups.
- <plans/purity_analysis.md> — purity annotation and redundant-hint follow-ups.
- <SELECTOR_BUGS.md> — matcher/diagnostic bugs with anonymized examples.
- <ARCHITECTURE_BACKLOG.md> — deeper refactors, urgent only when they block this
  queue.
- `perf/` — measured performance notes. Update from real
  profiles before major matcher/index rewrites.

Planning hygiene: keep active dispatch order here. When a plan's core work is
complete, summarize only its remaining tail here instead of leaving the plan as
a second priority queue.

### Remaining dispatch order

Safety evidence outranks speculative deletion counts. Source inspection is not
an end-to-end reproduction; performance candidates require fresh opt profiles.
The sections below hold details, not competing priority queues.

1. **Context-aware JavaScript names and vendor emission.** Distinguish binding
   identifiers from exported property names before deduplicating validators;
   replace unsafe wrapper interpolation with AST construction and parser/Node
   regressions. A blanket reserved-word ban would reject valid export names.
2. **Strict spec automation.** Keep next-version porting the product target.
   Prioritize correct edits and explicit skipped-candidate reasons. Whole-document YAML reserialization, unrelated
   formatting changes and dropping comments are acceptable: text preservation
   is not a requirement. Keep gate-before-write, atomic writes and dry-run
   reporting, not the input's textual layout.
3. **Public browser smoke.** Build <plans/excalidraw_live_smoke.md> and cover
   bundled vendor singleton/importmap behavior. Node probes already run;
   browser loading is the missing contract, not a missing vendor schema.
4. **Profile-backed AST and lookup work.** Measure cloning, file-name lookup
   and alpha-binding snapshots before ownership redesign, indexes or undo logs.
   Preserve ordering/hygiene and the independent Node differential oracle.
   Anonymous uniqueness uses coarse semantic-token buckets with exact equality;
   profile real workloads before adding more fingerprint machinery.
5. **Remaining selector authoring gaps.** Follow <SELECTOR_BUGS.md> and the
   minimizer work below. Solver-domain gap reachability still needs investigation;
   keep near-miss ranking heuristics distinct from boolean backtracking.
6. **Remaining responsibility seams.** Extract vendor/lowering boundaries only
   when they simplify real callers. Prefer deleting redundant setup over deleting
   behavioral assertions or adding test frameworks.

### Porting to the next bundle version

Each spec is for one version's bundles; a new version's spec starts as a copy of
the previous one. Make repairing that copy cheap:
<plans/automated_spec_workflows.md> § Flow 3, with the held-out
`debundle_stabilize` evaluation built on it.

### Automation product flows over the solver

Design and milestones: <plans/automated_spec_workflows.md> — patch-plan bulk
codemods that explain every skipped candidate and prove through the one
resolve, repair from the keep-going report, the inventory/plan/apply/validate
CLI model, and new-app bootstrap. Every flow is held to the interactive budget
in <docs/selector_resolution.md> § Interactive budget.

1. **Selector diagnostics.** Sharper first-mismatch reasons for `no_match`'s
   `nearest_unclaimed`: the first unmatched item of a multi-declaration range,
   the first incompatible identifier binding or sub-expression, a
   parameter-pattern mismatch, and list-hole binding spans; and near misses
   for multi-statement templates. Outcomes also do not yet record name-pin debt
   annotated with `note:`, or `alpha_all` readable names that are free
   references rather than local binders.
2. **Selector-debt ranking improvements.** Extend `debundle spec selector-debt`
   with source-aware ranking for multi-statement windows and "stable literal by
   value" candidates. Prefer output that can feed the patch-plan dry-run.
3. **Cross-module binding groups.** `source_matches[]` entries export every
   binding of one matched context into one logical module. Design a form for
   one matched context whose bindings land in different modules, without
   repeating the selector body.
4. **Explain unsatisfiable selector groups.** An infeasible group makes every
   selector in it `no_match` with one fixed reason (<docs/selector_resolution.md>
   § Unsatisfiable programs), so an author cannot tell which selectors clash.
   Name them without the solver: once the plain solve has proved infeasibility
   (milliseconds), explain it from the candidate sets in Rust. A Hall violator
   (k targets whose candidate places number fewer than k) should cover the usual
   authoring error, two selectors claiming one declaration; check that against
   real infeasible groups, and keep the group-level message for infeasibility
   that comes from relations. Assumption-core localization in the solver is
   rejected (<docs/selector_resolution.md> § Rejected: localizing a contradiction
   with assumption cores). The solver-level `ClaimOutcome` has no conflict
   variant: `conflict` is produced by reference narrowing before the solve, so
   explaining an infeasible group adds a solver-level variant and its match arms.

### Test infrastructure

1. **Public real-bundle smoke.** Build the Excalidraw live-browser smoke
   (<plans/excalidraw_live_smoke.md>) so private-corpus debundler issues can be
   reproduced and protected in public CI.
2. **Ground selector-stabilization skill fixtures.** Add tested, anonymized
   fixtures for the common anchor-choice cases of the `debundle_stabilize`
   playbook so its guidance is executable rather than only prose.

### Pipeline performance and architecture cleanup

Proposer-gate, `debundle run` (report opt-out, chunk-level incremental
rebuilds, codegen cache), and materialize-stage performance work lives in
<perf/proposer.md>.

1. `JsChunk::{get_file,remove_file}` (`artifacts/artifact.rs`) are linear
   scans over `files`, so passes that touch every file go O(n²) per chunk.
   Replace them with a path-keyed index if fresh profiles show chunk file
   lookup hot.
2. `split_entry_body` (`lowering/lower.rs`) borrows the prepared source AST.
   That AST is also consulted by `module_output::anonymous_statement_comments_by_span`
   and carries source maps/hygiene into output. A draining split is therefore
   an ownership-boundary change, not a local `iter()` → `into_iter()` edit.
   The residual-item helper now returns `Option<ModuleItem>` (no temporary
   result vector or fictitious error path). Profile the remaining required
   copies before redesigning source ownership.

### Read-off minimizer polish

1. **Dogfood-apply on the private downstream repo.** Run `synthesize-selectors --apply` on
   the real spec to convert the large set of fragile name-pins into robust
   `source_match` selectors, review for over-pin, and PR the beneficial ones.
   Revert any converted selector whose `match` block is >40 lines and has <=2
   holes back to a name pin. Keep pin-compatible with the released debundler
   expected by the downstream corpus validation flow, regenerate goldens, and
   re-measure selector debt after each batch.
2. **Undo-log alpha bindings in the matcher.** `selector_match`'s `Bindings`
   (a stack of `AlphaScope` frames of `HashMap`s) is cloned to snapshot before
   each backtracking alternative. If fresh profiles of whole-spec
   `synthesize-selectors --apply` show that cloning hot, replace
   clone-on-snapshot with an undo log and switch the maps to `FxHashMap`.
3. **Skip neighbor-borrowed uniqueness.** When a candidate's uniqueness comes
   only from a non-target neighbor (the target's own body fully holed, a
   neighboring declaration pinned), skip it with a reason instead of reporting
   `would_change`. Key the check on that property, not on selector length:
   neighbor-borrows as short as two lines slip under the >40-line over-pin
   heuristic.
4. **Say why a `--candidates N` menu is short.** `synthesize-selectors
--candidates N` returning one candidate does not say whether only one anchor
   exists or the menu is not enumerated for that shape (seen on an empty
   `class X extends Y {}`). Distinguish the two in the JSON.

## Anonymous selector indexing in graph dumps

Today `anonymous_statements:` selectors resolve purely by AST-shape match
against the chunk's top-level statements (`match` / `source_match`); the
spec format has no owner field for them, so nothing leans on
author-provided owner hints. That keeps the format honest, but every edit
gate / coverage check touching anonymous statements has to re-parse source.

If that source resolution ever becomes too expensive, extend
`owner_graph.json` with enough machine data to resolve anonymous selectors
from the dump itself — a canonical emitted-JS string or AST fingerprint per
anonymous owner, keyed by owner id and statement ordinal — so CLI tools can
match `anonymous_statements[].match` against graph-owned statements without
reading source files. This is conditional on hitting that cost; not yet
observed.

## Multi-chunk bump aids

Only when a multi-chunk bump needs them:

- A selector that no longer matches in its chunk but matches in another gets a
  hint naming that chunk.
- A cross-chunk `same_as` relation for mirrored module trees.

## Purity classifier

Statement-level overrides, the redundant-hint guardrail and compositional proof:
<plans/purity_analysis.md>.

The ignored `inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle`
fixture is a deferred **RegExp admission feature**, not a contradictory classifier
bug. `purity/whitelists.rs` explicitly requires static ECMA-262 pattern validation
before admitting literal `RegExp` construction; the active classifier test
correctly expects Unknown today. Keep the future test ignored until that
precondition is implemented; do not whitelist constructors that can throw.

## Rename pipeline

`lowering/rename_ledger.rs`'s module doc is the architecture reference for
the collect → seal → execute-once `RenameLedger` pipeline. Ideas it unlocked,
still open:

- **Id-keyed rename executor.** Requirements and tripwire: <lowering/rename_ledger.rs>
  module doc § Hygiene boundary.
- **Aggressive auto-naturalization.** Now safe to build: every rename
  flows through one seal, so a new auto-naming heuristic contributor
  (readable names for still-minified bindings, driven by the
  unrenamed-symbol priority-queue side output) only needs to submit
  intents plus deriving-subtree facts — conflict resolution, occupancy,
  and capture validation come for free, and the
  renamed-it/didn't-notice miscompile class (#2045) is unrepresentable.
- **Type-level structural-move barrier.** The "no structural moves
  between seal and execute" contract is convention-held; making
  non-execute passes take `&Module` would let the compiler enforce it.

## Suspected bugs

Findings from a read-only code review (2026-09-30), each with where to look and
how to confirm. An entry is deleted when it is reproduced and fixed with a test,
or disproved. Status says how far it was checked; "reported" means nobody has
confirmed it. Selector-matching findings are in <SELECTOR_BUGS.md>.

- **Vendor name validation accepts reserved words.** Status: function read,
  reachability not checked. `vendor/mod.rs` `is_valid_identifier` accepts
  `class`, `default` and `await`; it gates namespace, local and facade names that
  `vendor/validate.rs` emits as binding names, so a reserved word would produce
  unparseable JS. `lowering/util.rs` `is_valid_js_identifier` rejects reserved
  words. Related, reported: `vendor/wrappers.rs` uses string interpolation to
  build `export const {name} = _d.{name};`, so a string-literal export name in a
  vendor chunk yields invalid JS (and bypasses the AST-only rule). Use one identifier
  policy with separate binding-identifier and IdentifierName contexts; do not
  reject valid string-literal exports merely to reuse a binding check.
- **One SCC reads differently by path in the peel kernel.** Status: reported,
  likely benign. The post-seed reporting in `peel/quotient.rs` keeps an SCC
  whose owners collapse into one class, while the `translate_*` helpers drop it
  and `would_be_cycles_after_contract` fabricates a two-class evidence.
- **`needs_ast_for_chunk` tests the opposite direction to its comment.** Status:
  reported. In `prepare_chunks.rs` the block commented "Chunk that imports a
  vendor target needs AST" checks whether a vendor target imports this chunk;
  the following block is the "imports" direction. The effect is over-retaining
  ASTs, not unsoundness.
- **Two definitions of "residual".** Status: reported. `reports/schema.rs`
  `ModuleEntry.residual` is documented as authoritative, not derivable from
  `path`, while `spec::is_residual_module_path`, `spec_stats` and the CLI derive
  it from the `residual/` prefix. Possibly intentional (authoring tree versus
  materialized), but undocumented.
- **CLI papercuts.** Status: reported. `gate list` and `gate cut` require
  `--graph` even with `--cycles`, which only needs it to derive a default path;
  `scc --cycles-only --singletons-only` silently returns nothing (no
  `conflicts_with`).

## CLI usability

Open usability and scripting-safety findings from exercising the documented
workflows against a real spec; resolved items are deleted. Corpus-specific
paths and owner ids belong in the consuming repo.

### Planner CLI follow-ups

- **Diagnostics toggle for `modules propose`.** `--limit` now bounds
  proposals and diagnostics and the `limits` summary reports totals
  when details are truncated, but there is still no explicit
  diagnostics on/off toggle for first-pass planning, where proposal
  rows are the only thing the caller wants.
- **Concise explain mode.** Proposal/diagnostic structures on
  `describe` are already opt-in (`--include-proposals`), but there is
  still no compact mode focused on: selected owner identity and source
  span, atomic-unit membership, matching proposal (if any), immediate
  constraining neighbors, and the exact reason the owner is not
  landable today.
- **Source roots.** `show-source --source-root ...` depends on the
  consuming target's source-tree layout. Runbooks and skills should make
  that target-specific root explicit instead of assuming repository root
  or working directory.
- **Patch-plan naming.** `coverage` is useful for intersecting existing
  module YAML with atomic-unit coverage, but it is not the only way to
  discover readable work: `atoms --readable-only` and `modules propose`
  may show graph-valid work even when no whole patch section is ready.
  Docs and skill text should reserve "plan" for proposed edits that can be
  reviewed/applied, and avoid implying that empty coverage output means there
  is no landable work.
