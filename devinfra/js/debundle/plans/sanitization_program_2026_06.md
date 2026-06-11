# Debundle sanitization program — 2026-06

A ~3-week program of structural cleanups following the 2026-06-11 full review
and its 16-PR fix wave (#2044–#2073). Goal: convert the remaining
convention-held invariants into structure, make module boundaries
compiler-enforced, collapse the tree-rewriting-era pipeline braid, and harden
the test infrastructure — so the next capability wave (more aggressive
naturalization, bigger corpora) lands on sane wiring.

Detailed item descriptions live in CODE_REVIEW.md / ARCHITECTURE_BACKLOG.md /
TODO.md (filed by #2073); this plan sequences them and adds the two designed
refactors (RenameLedger, vendor-into-emission). Delete each backlog entry as
its work lands; tombstone this plan when the program completes.

## Entry criterion (week 0): real-corpus validation

Before structural work starts, re-peel the real downstream corpus
(gaffer-private snapshot regen) on current `devel`. The fix wave made
analyses stricter (purity coercion, admission checks, at-init fallback) and
changed gate acceptance in both directions; synthetic e2e is green but the
real bundle is the test. Expected fallout is over-restriction — resolved by
`purity:` / `pure_members` / `admission_overrides` annotations or safe
whitelist growth, never by weakening the analyses. This run also validates
the #2045 naturalization fix on the bundle that motivated it and gives a
perf baseline for the program.

## Track A — wiring coherence (week 1)

1. **Stale `ArtifactIndexes` fix (S, immediate).** `pipeline.rs` builds
   indexes once post-prepare and reuses them for partial swaps after
   materialize replaced files and full swaps removed chunks (materialize
   already rebuilds its own internally). Fix: rebuild after any artifact
   mutation — and make the discipline structural: mutation entry points
   consume `(artifact, indexes)` and return fresh ones, so stale reuse stops
   typechecking.
2. **Crate split (M).** Split the `:analysis` god-crate (~29 srcs spanning
   facts/graph/purity/realizability/validation/reports/stage_one) into
   per-subsystem `rust_library` targets; then the `realizability.rs` internal
   split (esm_simulator, IncrementalQuotient, tests; `gate_perf_counters`
   only after untangling its index entanglement — see CODE_REVIEW). Pure
   moves, no behavior change; makes the wished-for boundaries
   compiler-enforced and shrinks rebuild times.
3. **Decision batch (maintainer, ≤1h total). _(Done 2026-06.)_** All
   four calls are closed: PK-gate vs realizability-index — resolved by
   the gate-ladder series (`plans/incremental_gate_unification.md`,
   tombstoned; PRs #2087/#2090/#2095/#2102 plus the perf-validation
   doc-sync pass), which routed the hot gate through the index's tier
   ladder and deleted the kernel PK walk; the unreachable multi-target
   fallback in `peel/quotient.rs` — deleted in #2102 (single-target
   assert in its place); `landable_today` for cross-residual proposals
   — blocked rows are not landable, implemented; `BindingId` interning
   — deferred, perf-triggered, see the backlog's decided-state note.

## Track B — RenameLedger (weeks 1–2) — complete (2026-06)

The structural fix for the rename-bug family (#2052/#2057 were
down-payments). Five PRs, each independently green against the ~90
rename-pinning e2e tests — all landed
(#2086/#2091/#2101/#2106 + the PR-5 cleanup):

1. Types + inventory: `RenameLedger` of `RenameIntent { scope, from, to,
origin, priority }`, scopes Chunk/Module/Function, **keyed by hygiene
   `Id`** (post-#2042 contexts are why string keys breed bugs). Adopt the
   "no structural moves between seal and execute" contract. _(Landed
   2026-06: #2086 — `lowering/rename_ledger.rs`, including the seal-time
   same-priority conflict check and the two explicit contributors —
   spec `chunk_renames` + plan `export_name`s.)_
2. Collect: convert contributors (spec `export_name`s, bound/free
   heuristics, import-local `_N` minting, `chunk_renames`, cross-module,
   collision resolution) one at a time to emit intents. _(Landed
   2026-06: #2091.)_
3. Seal: explicit > import-induced > heuristic; same-priority conflicts are
   hard errors naming both contributors; target-occupancy validated at seal
   time against scope-accurate occupied sets; `_N` minting becomes a ledger
   service. _(Landed 2026-06: #2101.)_
4. Execute once: the post-seal rename pass is the only mutation per scope
   unit; capture facts reach seal from the un-renamed tree. _(Landed
   2026-06: #2106 — read-only `RenameCaptureProbe` + `pending_renames_by_name`
   replace the pre-seal trial walks, the export-growth ledger merged into
   `lower_chunk`'s chunk ledger, and `plan_references`' reverse `.find`
   became the sealed map's inverse projection.)_
5. Cleanup: delete the defensive era and resolve PR 4's documented
   blockers. _(Landed 2026-06: the dominated `drop_subtree_captured_targets`
   subtree re-walk and the clone-side capture asserts are deleted (the
   post-seal executors keep the single `debug_assert!` tripwire layer);
   the import-ledger merge and `cross_module_chunk_renames` folding are
   finalized as design — the cross-module phase cycle and the sequential
   mint composition are documented invariants in `rename_ledger.rs`'s
   "The four ledgers" / "Boundaries that validate at application" — as
   are the derive clone and the `merged`/`explicit` map split. The
   `Id`-keyed executor is re-filed in TODO.md "Rename pipeline" (blocked
   on real-context import/export emission). The #2045 class is
   unrepresentable; aggressive auto-naturalization is now a TODO.md
   capability item.)_

## Track C — vendor-into-emission collapse (weeks 2–3)

The one genuinely unnatural pipeline ordering left: vendor ops braided
around materialization (full swaps before, partial swaps + strip after),
three artifact-mutation waves, duplicate specifier-construction paths.
Target shape (design.md "Pipeline trajectory" direction; its stated
precondition — gate/planner unification — landed in #2071):

1. Design doc first (separate `plans/` entry with the module-graph
   modeling: a swapped vendor package becomes an external `ModuleId` the
   import planner targets; partial-swap reference rewriting moves into
   import planning; strip becomes vendor emission).
2. Staged implementation behind the existing e2e suite (vendor_swap_test +
   the #2062 fixtures are the safety net).
3. Fold `rewrite_chunk_entry_specifiers`' and lowering's parallel specifier
   construction into one path; retire `validate_emitted_exports` only if the
   collapse makes it provably redundant (else keep as tripwire).

Also in this track (independent, can parallelize): vendor code quality from
CODE_REVIEW — validate/resolve dedup into `vendor/validate.rs`, manifest
consolidation (`ResolutionManifest<R>`), typed chunk identity.

## Track D — type-shape and strict-mapping cleanups (week 2, parallel)

- `StatementFacts`: `PositionBucketed<T>` (9 fields → 3, structural subset
  invariants), unify the `StructuralStatementFacts` vocabulary, derive
  `effects`.
- `ChunkManifest` → `#[serde(flatten)]` over `ChunkAnalysisReport` (zero
  wire change; resolves the backlog's auto-derive open decision).
- `ChunkAnalysis` field privatization (cache-staleness hazard).
- Fold `program_analysis.rs`'s remainder into the facts walk (two parallel
  extractors with divergent traversal rules).
- `graph.rs` strict mapping: error on duplicate top-level declarations and
  on `from_report` edges with unresolvable endpoints.
- `cli/mod.rs`: move scc/cluster implementations + renderers out.

## Track E — CLI scripting surface (week 2–3, parallel)

From TODO's new section, sized S–M each: structured JSON gate-rejection
output on stdout (agents currently scrape stderr prose); `--format` parity
for `modules {merge,delete}` (one outcome schema across the five mutating
verbs); emit `cycles.json` on edit-gate and `--dry-run` rejections so the
documented `gate list/describe` follow-up works; split
`DEBUNDLE_SOURCE_ROOT`'s double meaning; `.ok()?` swallow and sentinel-hack
fixes.

_Status (2026-06-11): landed — structured rejections + shared
`MutationOutcome` schema (`cli/outcome.rs`), rejection artifacts on
edit-gate and `run --dry-run` rejections, `DEBUNDLE_TREE_SOURCE_ROOT`
split, `.ok()?` and sentinel fixes. TODO section removed._

## Track F — test infrastructure (week 3)

1. **Randomized differentials**: proptest-generated owner graphs +
   push/undo/contract sequences asserting incremental index ==
   `check_realizability`, with an _independent_ reference partition builder
   (the flagship differential currently shares the kernel's own
   projection); cover the gate-residual promotion transition.
   _Status (2026-06)_: proptest is wired into the Rust/Bazel build
   (`@crates//:proptest`) and the first property suites landed —
   `realizability/condensation_order_proptest.rs` (digraph
   mutation/overlay sequences vs `tarjan_scc` brute force) and
   `lowering/rename_ledger_proptest.rs` (public seal contract). The
   remaining F1 step is migrating the gate-differential harness
   (`peel/gate_differential_test.rs`) from its deterministic xorshift
   sweep to proptest strategies — unblocked now that the gate-ladder
   series has landed (the harness already asserts strict
   gate-vs-reference equality post-cutover).
2. **Lemma pinning**: named tests for Lemmas 1/3/4/5 (only Lemma 2 has
   them); extend the #2071 Node-differential to a fixture sweep.
3. **Excalidraw public smoke** (TODO's big standing item): the
   realistic-corpus CI layer the fix wave repeatedly found missing
   (`props/frontend/debundle` no longer exists). Build the Bazel-managed
   bundle + minimal spec + live-proxy assertion per the existing TODO
   design. This is the largest single item; it pays for itself the first
   time a private-corpus regression reproduces publicly.
4. Small test debt: support.rs builder consolidation, hardcoded entry path
   in `assert_generated_module_after_entry_script`, whitespace OR-chain
   assertions → AST helpers.

## Sequencing constraints

- A1 lands first (it's a latent-bug fix, everything else churns the same
  files). A2 (crate split) before B/C/D where possible — pure moves rebase
  painfully _under_ semantic PRs, not over them.
- B and C touch lowering from different sides (renames vs imports); run B
  PRs 1–3 before C's implementation starts, or accept one rebase round.
- D and E are conflict-free with everything except themselves.
- F is independent; F3 (Excalidraw) can start any time.
- Every PR deletes its backlog/TODO entries in the same commit; anything
  consciously dropped gets re-filed, not silently lost.

## Non-goals

No init-wrapper machinery, no runtime checks, no compatibility envelopes,
no weakening of the soundness rule to make a corpus pass — these remain
design invariants. The factor-vocabulary rename
(`plans/factor_vocabulary_rename.md`) stays deferred; it conflicts with
every track and should run as a dedicated wholesale pass after the program.
