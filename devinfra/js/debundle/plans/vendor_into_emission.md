# Vendor-into-emission: collapsing the vendor waves into plan/emit

Design for Track C of `plans/sanitization_program_2026_06.md` — the last
genuinely unnatural pipeline ordering: vendor operations braided around
materialization (full swaps before, partial swaps + strip after), three
artifact-mutation waves, and duplicate specifier-construction paths. The
target follows docs/design.md "Pipeline trajectory": vendor
classification stays an input-space concern; swap and strip become
**emission decisions**. The stated precondition (gate/planner
unification on the shared realizability primitive) landed in #2071 and
the gate-ladder series (`plans/incremental_gate_unification.md`,
tombstoned); the artifact-index threading this design leans on landed
in #2077 (`IndexedArtifact`).

Design-only: no production code changes in this PR. Implementation is
staged in §8 behind `e2e/vendor_swap_test.rs` (including the #2062
consumer-gate and collision fixtures, and #2084's
`default_export_aliases` coverage).

## 1. Current state: the braid

`pipeline.rs` runs, in order (each `IndexedArtifact::update` rebuilds
`ArtifactIndexes` from the mutated bundle):

| #   | stage                                | mutates                                                    | why it sits where it does                                                                                                             |
| --- | ------------------------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| 0   | `rewrite_chunk_entry_specifiers`     | import/`import()`/`Worker` directives in every AST file    | always-on canonicalization; downstream stages assume canonical specifiers                                                             |
| 1a  | `rename_vendor_exports`              | every caller AST file (vendor-local → public import names) | must precede materialize so lowered bodies inherit the renamed imports                                                                |
| 1b  | `swap_vendor_chunks`                 | **removes** full-swap chunks; writes wrappers + manifest   | must precede materialize so the rest of the pipeline never sees removed chunks (`chunk_records.retain` on `removed_chunk_ids`)        |
| 2   | `materialize_logical_modules`        | replaces chunk files with lowered module files             | —                                                                                                                                     |
| 3a  | `apply_partial_vendor_swaps`         | every AST file (per-symbol consumer rewrites)              | must **follow** materialize: the ident rewrite (`zodObject(...)` → `z.object(...)`) would erase binding names spec selectors match on |
| 3b  | `apply_bundled_partial_vendor_swaps` | every AST file + vendor-chunk self-rewrite (facade import) | same, plus emits the facade bundle                                                                                                    |
| 3c  | `strip_swapped_vendor_exports`       | vendor entry ASTs (export strip + reachability sweep)      | needs 3a/3b's `replacement_import_locals` and the post-rewrite consumer state                                                         |
| —   | `validate_partial_swap_consumers`    | read-only post-strip scan (the #2062 gate)                 | catches consumer shapes 3a/3b had no live rewrite for                                                                                 |
| —   | `validate_emitted_exports`           | read-only duplicate-public-export tripwire                 | catches emit-shape regressions that browser-link silently                                                                             |

Three vendor mutation waves (0+1, 3) bracket materialize; seven artifact
mutations and eight index builds per run. Three independent
specifier-resolution/construction paths exist:

1. **Stage 0's** `RuntimeSourceRewriter` — resolves relative sources
   through `ArtifactIndexes::resolve_runtime_import_reference` and
   rewrites directives in place, pre-materialize.
2. **Lowering's** construction path — `import_emit::relative_source` /
   `import_decl_for_plan` plus `runtime_imports.rs`'s re-import
   builders, constructing fresh directives for lowered module files.
3. **The partial-swap dispatchers'** `MaterializedOutputChunkIndex` —
   a longest-prefix-match index rebuilt per invocation because
   post-materialize module files have output paths the per-symbol
   rewriter must re-map onto chunks, duplicating resolution stage 0
   already performed.

The braid is held together by ordering comments ("partial-swap runs
_after_ materialize so …") rather than structure. The post-hoc gates
(`validate_partial_swap_consumers`, `validate_emitted_exports`) exist
because no single component can see the whole decision.

## 2. Target model

Three layers, replacing the waves:

- **Classify (input space, unchanged in nature).** The `vendor` spec
  map and its parse-time guarantees (`VendorLevel` variants carrying
  exactly their required fields, #2084's `default_export_aliases`)
  stay an annotation/identity concern. Validation of marks against
  chunks (unknown chunk, missing entry AST, alias names the chunk
  doesn't export, version mismatches against installed packages)
  happens **once**, immediately after `prepare_js_chunks`.
- **Plan (read-only).** A `VendorResolutionPlan` is computed once from
  the prepared artifact + indexes: per-chunk disposition (kept /
  external / partially-external), boundary mappings, per-symbol
  external targets, wrapper/facade shapes, and the consumer-shape
  classification that is today's post-strip gate. Import planning —
  both lowering's per-plan construction and the directive rewriting
  for non-materialized files — consults this plan as the **single
  resolution oracle**.
- **Emit (writes outputs).** Swapped chunks are an emission-set
  exclusion, not an artifact mutation. The residual of a
  partially-swapped chunk (today's strip) is computed while emitting
  that chunk. Wrappers, facade bundles, and vendor manifests are
  emission outputs (they are already write-gated behind
  `swap_vendor_chunks.write`).

### 2.1 Per-`VendorLevel` mapping onto partition / import-planning concepts

| `VendorLevel`          | chunk disposition                        | consumer references                                                                                                                                                                                                    | partition / gate view                                                         |
| ---------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `suppress`             | emitted pass-through                     | untouched                                                                                                                                                                                                              | not in any owner graph (vendor chunks are never in `logical_modules`)         |
| `boundary_rename`      | emitted pass-through                     | import-directive **name mapping** (vendor-local → public) applied at construction/rewrite time from the plan's `collect_boundary_mapping` result                                                                       | same                                                                          |
| `swap` (full)          | **excluded from the emission set**       | directives keep the chunk specifier (dangling-for-live-proxy contract, pinned by `full_swap_with_caller_keeps_dangling_chunk_import_for_live_proxy`) with boundary mapping applied; wrapper emitted per `WrapperShape` | external leaf — never appears in `I`/`S`; A7 verified in §2.3                 |
| `partial_swap`         | emitted as **computed residual** (strip) | per-symbol: `named`/`default`/`namespace` kinds → import-directive surgery targeting the package; `member` kind → body reference rewrite to a namespace member access; both driven by the plan's symbol table          | residual chunk unchanged in role; swapped symbols resolve to an external leaf |
| `bundled_partial_swap` | emitted as computed residual + facade    | as `partial_swap`, but member-addressable through the facade default; the vendor chunk's own **self-rewrite** (facade import + internal reference re-targeting) becomes the first step of its residual computation     | same; the facade bundle is an external leaf                                   |

`default_export_aliases` (#2084) stays exactly what it is: an
author-asserted input to full-swap wrapper-shape validation
(`named_from_module_default`'s verified-alias check in
`vendor/mod.rs::swap_vendor_chunks`). It moves from swap-stage
validation to plan-time validation — the only change is _when_ a bad
alias fails the run (before any output instead of mid-pipeline).

### 2.2 External module identity: `ImportTarget`, not `ModuleId::External`

The import planner's lists are today `Vec<(ModuleId, ModuleItem)>`
ordered by `EsmImportOrder`. The collapse needs a way to say "this
import targets a swapped package" in the same vocabulary. Two options:

1. Add an `External` variant to `ModuleId`.
2. Introduce a planner-boundary enum and leave `ModuleId` alone:

```rust
enum ImportTarget {
    Module(ModuleId),        // intra-chunk logical module (incl. residual)
    Chunk(ChunkId),          // cross-chunk artifact target (runtime re-import)
    External(ExternalId),    // swapped package: interned (package, subpath)
}
```

**Recommendation: option 2.** `ModuleId(LogicalModuleIndex)` is a dense
index — `Partition::of` is a `Vec<ModuleId>`, the realizability index
and `EsmImportOrder` key dense tables by it. An `External` variant
would poison every dense consumer with a partial domain for the sake of
one boundary. `ImportTarget` lives where the heterogeneity actually is
(the planner's import lists and the directive rewriter) and the gate
never sees it (§2.3).

Ordering: `EsmImportOrder` needs **no semantic change**. Today,
intra-chunk imports are sorted by the shared rule and runtime
re-imports "follow; they're outside the per-chunk I-graph the gate
reasons about" (`lowering/lower.rs`). `External` targets order exactly
where the references they replace sat:

- in module files, package imports join the runtime re-import block
  (after sorted intra-chunk imports, deterministic by
  `(package, subpath)`) — matching today's post-hoc rewrite, which
  rewrites the runtime re-import block in place;
- in entry / pass-through files, the original directive is rewritten
  **in position** (today's `rewrite_swap_import_decls` behavior),
  preserving the source bundle's vendor side-effect evaluation order.

### 2.3 The owner graph and the gate: verifying A7

Vendor chunks were never part of any per-chunk owner graph — `I` and
`S` are per-chunk; cross-chunk imports are black-box leaves (design.md
"Multi-chunk extension", A7). The collapse must keep that true:

- An external package has **no out-edges into the chunk graph**: npm
  code cannot import a debundled chunk (no such specifier exists in
  the package). Consumer → external edges are sinks; the multi-chunk
  union graph stays a DAG. A7 holds by the same argument that covered
  the pre-swap vendor chunk — strengthened, in fact, since a real npm
  package provably cannot reach back where a vendor _chunk_
  theoretically could.
- The gate simulator's node universe is intra-chunk `ModuleId`s; the
  `EsmImportOrder` it shares with the emitter is over the same
  universe. `ImportTarget::External` never enters either. No gate
  behavior changes.
- Evaluation-order preservation: the package's module-init side
  effects fire at the consumer's import-directive position — the same
  position the swapped chunk's init fired from, because both
  application sites (§2.4) preserve directive position. This is the
  same observational-equivalence contract today's in-place rewrites
  rely on, now stated once instead of implied by three rewriters.

### 2.4 One specifier-construction path

"One path" means **one resolution oracle**, consulted by two
application sites:

- **Lowering (construction).** `plan_module_reference_needs` /
  `record_runtime_imports` / `source_chunk_imports_for_moved_body`
  consult the `VendorResolutionPlan` when a referenced binding or
  source-chunk import resolves into vendor territory: boundary-renamed
  names are constructed correctly the first time; partial-swap symbols
  produce `ImportTarget::External` imports and (for `member` kind) a
  per-plan body-replacement map `Id → Replacement{Ident|Member}`
  applied alongside the existing per-plan visitors
  (`rewrite_runtime_sources_for_target` is the model: a lowering-side
  visitor fed from typed plan data). Note this is _not_ a
  `RenameLedger` entry — ident→member-expression replacement is an
  expression rewrite, not a rename; it stays a separate plan-driven
  visitor, sequenced after the sealed rename application like the
  runtime-URL rewrite is today.
- **Pass-through emission (rewrite).** Files that are emitted without
  lowering (entry residuals' retained directives, suppress /
  boundary-rename / residual vendor chunks, non-materialized chunks)
  get **one** position-preserving directive rewriter at emit time that
  performs, in a single visit: specifier canonicalization (stage 0's
  job), boundary-rename name mapping (stage 1a's caller side), and
  partial-swap directive surgery (stage 3a/3b's consumer side). All
  three consult the same oracle; `ArtifactIndexes` remains the
  resolution substrate (`resolve_runtime_import_reference`), and
  `MaterializedOutputChunkIndex` dies — the planner knows which module
  file came from which chunk by construction instead of re-deriving it
  from output paths.

`ast_has_rewritable_specifier` keeps its role in `prepare_js_chunks`
(AST-drop safety) — with emission-time rewriting it becomes the
guarantee that AST-less files need no emission rewriting at all, which
is also the coverage argument for the plan-time consumer gate (§3.2).

### 2.5 Strip becomes vendor emission

`strip_swapped_vendor_exports` stops being an artifact mutation wave
and becomes the **residual computation** inside the vendor chunk's
emission function:

```text
emit_vendor_residual(chunk):
    module  = prepared entry AST (clone at emit; input space untouched)
    module  = directive rewrite (canonicalize; §2.4)          # was stage 0
    module  = self-rewrite (bundled facade import + re-target) # was 3b's seed
    module  = strip(module, plan.symbols, plan.replacement_import_locals)
              # split var decls, strip export specifiers, sweep — unchanged
    render
```

This is the _same composition_ the pipeline executes today, locally
sequenced inside one function instead of braided across four stages
with index rebuilds between. The composition becoming structural (a
function body, not a stage order) is the core robustness win.

The full-swap side analogously: `swap_vendor_chunks` stops removing
chunks from the artifact. Exclusion is a property of the emission set
(`write_js_tree` / harness emission skip the chunk; reports derive
from the plan), and wrapper emission joins the emit phase. The
`removed_chunk_ids` retain-dance in `pipeline.rs` disappears.

### 2.6 The dissolved ordering constraints

- _"Partial swaps must run after materialize or the rewrite erases
  binding names selectors rely on"_ — dissolves structurally: spec
  selectors always match prepare-time input-space ASTs (which nothing
  mutates any more); vendor replacements apply only to lowered bodies
  and emission outputs.
- _"Full swaps must run before materialize so the owner graph doesn't
  see removed chunks"_ — dissolves: nothing is removed; the owner
  graph never contained vendor chunks; the emission set is where
  exclusion lives.
- _"rename_vendor_exports must precede materialize"_ — dissolves: the
  boundary mapping is an oracle input to lowering's import
  construction rather than a pre-pass over caller ASTs.

## 3. Soundness gates in the new position

The rule: no gate weakens. Position changes; predicates and the module
shapes they run on do not.

### 3.1 Strip-internal gates (split-brain, observable-effect privacy, live-reads, export-surface invariance)

All four live inside `strip_one_chunk_with_replacement_imports` /
`sweep_unreachable_top_level` and move with it, unchanged:

- **Split-brain bail** (single-package reachable-from-residual items)
  and its two documented bypasses (multi-package items,
  `shareable_helper`s) — design.md "Deliberate split-brain bypasses".
- **Observable-effect privacy bail** (swap-reachable side-effect items
  whose writes are not provably swap-private).
- **Live-item-reads-dropped-decl bail** (fixpoint soundness check).
- **Export-surface invariance** (Phase 2 must not change Phase 1's
  export set; swapped names must not leak).

Input-equivalence argument: today the gates run on the entry AST after
stage 0's canonicalization and 3b's self-rewrite, with
`replacement_import_locals` from the bundled dispatcher. §2.5's
composition feeds them the same module shape and the same inputs —
the difference is that the sequencing is enforced by one function
instead of by pipeline order, so a future reordering bug becomes
unrepresentable rather than latent. Pinned by the existing strip
fixtures (`partial_swap_rejects_split_brain_residual_reachability`,
`partial_swap_rejects_observable_side_effect_reading_swapped_binding`,
`partial_swap_keeps_side_effect_init`, …).

One deliberate non-change: strip re-analyzes the post-self-rewrite
module with `analyze_chunk` rather than consuming prepare-time
manifest facts. That stays. The AGENTS.md no-phase-local-rescan rule
governs _input-space_ facts; the residual computation runs on a
module that differs from input space by the self-rewrite, so its
facts are legitimately local. (Open question 5 records the rejected
alternative.)

### 3.2 The #2062 consumer gate

Today: a post-strip artifact-wide scan over every retained AST file,
bailing on surviving references to the stripped export surface
(unrewritten named imports/re-exports, namespace imports, `export *`,
`member`-kind / bundled re-exports).

New position: **plan time**. The `VendorResolutionPlan` enumerates
every consumer directive targeting a partially-swapped chunk while
classifying it for rewrite; shapes with no live rewrite are rejected
before any emission. Coverage is equal by construction:

- Today's gate reads `file.ast()` only — AST-less files are skipped.
- `prepare_js_chunks` drops an AST **iff**
  `ast_has_rewritable_specifier` is false, i.e. the file contains no
  relative-source directive — and chunk imports are relative, so an
  AST-less file cannot reference a vendor chunk.
- The planner enumerates directives from the same AST set. Same
  universe, same shapes, same bail messages.

The plan-time form is strictly _earlier_ (fails before writing
anything) and structurally _harder to bypass_ (a consumer the rewriter
misses is impossible — rewriting and gating are the same
classification). During the transition (PRs 3–5) the post-emission
scan **stays on** as a differential tripwire; PR 6 retires it only
after the #2062 fixtures demonstrate the plan-time gate fires on every
case the post-hoc gate did. Over-restriction remains the accepted
failure mode (a namespace consumer reading only unswapped members is
still rejected).

### 3.3 `validate_emitted_exports`

**Ruling: keep, as a tripwire over emission outputs.** The collapse
eliminates one duplicate-export source class (vendor waves can no
longer interleave with materialize's export planning), but it does not
make duplicates provably unrepresentable: the auto-grown residual
export block and `chunk_renames`-driven export aliases are still
planned in different lowering phases, and the check guards _every_
emit path including future ones. It is a cheap full-output static read
whose failure mode it guards (Chromium's silent blank-page link
failure) is expensive to diagnose. Re-point it at the emission outputs
(same walk, same diagnostics); revisit deletion only if a later
single-export-registry refactor makes per-file export planning
genuinely single-sourced.

## 4. Mutation-wave accounting

Before (per `pipeline.rs` with #2077's threading):

| wave                          | artifact mutations | index rebuilds              |
| ----------------------------- | ------------------ | --------------------------- |
| 0 specifier canonicalization  | 1 (all AST files)  | 1                           |
| 1 vendor pre-wave (1a, 1b)    | 2                  | 2                           |
| 2 materialize                 | 1                  | 1 (+ internal)              |
| 3 vendor post-wave (3a,3b,3c) | 3                  | 3                           |
| **total**                     | **7**              | **8** (incl. initial build) |

After:

| phase     | artifact mutations                          | index rebuilds |
| --------- | ------------------------------------------- | -------------- |
| prepare   | 0 (build `IndexedArtifact` once)            | 1              |
| classify  | 0 (validation reads)                        | 0              |
| plan      | 0 (`VendorResolutionPlan`, lowering plans)  | 0              |
| emit      | 1 (materialize remains the single mutation) | 1              |
| **total** | **1**                                       | **2**          |

Vendor contributes **zero** mutation waves. Materialize's wave stays —
collapsing materialize-into-emit (lowered outputs feeding
`write_js_tree` without a bundle round-trip) is the _next_ step of the
design.md trajectory and is explicitly out of scope here; this design
removes the vendor braid around it, which is what made that next step
impossible to even state.

## 5. Wire / report compatibility

Consumers: the combined `VendorSwapsReport` JSON
(`swap_vendor_chunks.output_manifest_path`), wrapper / facade files
(`output_wrapper_dir`), and e2e assertions over both.

- **Shapes preserved**: `VendorResolution`,
  `ChunkPartialSwapResolution`, `ChunkBundledPartialSwapResolution`,
  `ChunkStripStats`, keyed by `chunk_path`, serialized as today
  (`full` / `partial` / `bundled_partial` / `strip_stats`). They become
  projections of the `VendorResolutionPlan` + emission results instead
  of per-stage by-products. Typed `ChunkId` threads internally; names
  convert at the wire boundary only (per the CODE_REVIEW item).
- **`references_rewritten`** is the one field whose _basis_ shifts:
  today it counts post-hoc AST rewrites; for materialized consumers it
  becomes the count of plan-driven constructions/replacements applied
  during lowering. The invariant to preserve: it counts **emitted**
  references (application-site increments, summed into the same
  per-symbol field), not planned candidates. PR 3 carries a fixture
  asserting count equality on the existing partial-swap fixtures.
- **Strip stats** are computed by the same sweep at its new position —
  byte-identical values expected; `partial_swap_keeps_megachunk_on_disk`
  and `partial_swap_strips_implementation_when_unreferenced` pin the
  observable behavior.
- Wrapper / facade bytes: unchanged generators
  (`vendor/wrappers.rs`), unchanged paths, now written during emit
  (they already only write under `swap_vendor_chunks.write`).

## 6. Vendor code-quality items (CODE_REVIEW): fold in, with sequencing

Ruling on the three parallel items — all three fold **into** this
series rather than running before or after it, at specific slots:

- **Validate/resolve dedup → `vendor/validate.rs`** (the ~250-line
  twin blocks in `apply_partial_vendor_swaps` /
  `apply_bundled_partial_vendor_swaps`): **PR 1, before any semantic
  change.** The `VendorResolutionPlan` _is_ the deduplicated
  validate/resolve logic with a name; extracting it first as a pure
  refactor makes PR 2 a move instead of a rewrite. Same rationale as
  the sanitization plan's "A2 before B/C" note — pure moves rebase
  painfully under semantic PRs, not over them.
- **Typed chunk identity** (`String` chunk names masquerading as
  `chunk_id` through vendor code): **PR 1.** Every later PR rewrites
  these call sites; doing the `ChunkId` threading first means the new
  seams are born typed instead of migrated twice. Names appear only in
  wire structs and diagnostics afterwards.
- **`ResolutionManifest<R>` consolidation** (field-for-field twin
  manifests; `PartialSwapSymbolTarget` twin of
  `spec::PartialSwapSymbol`): **PR 2**, where the manifests become
  plan projections — the generic falls out of giving them a single
  producer, and doing it earlier would churn the same lines twice.

Each PR deletes its CODE_REVIEW entries in the same commit.

## 7. What gets deleted at the end

- **`pipeline.rs`**: `run_full_vendor_swaps`, `run_partial_vendor_swaps`,
  the `removed_chunk_ids` / `chunk_records.retain` dance, the
  partial-after-materialize ordering comment, and the stage braid they
  orchestrate. The pipeline body becomes: load → prepare → classify →
  plan → emit → reports.
- **`rewrite_specifiers.rs` as a stage**: `RuntimeSourceRewriter`
  relocates into the pass-through emission rewriter;
  `ast_has_rewritable_specifier` stays with prepare;
  `RewriteChunkEntrySpecifiersManifest` dies (its counts fold into the
  emission report).
- **`vendor/mod.rs`**: `dispatch_partial_swap_jobs` and the per-file
  job plumbing (`PartialSwapFileJob`/`Result`, `DeferredImport`,
  `IdentRewriteTarget` as a post-hoc artifact-wide pass),
  `MaterializedOutputChunkIndex` (with its two-shape longest-prefix
  match), `rename_vendor_exports`'s artifact-wide caller sweep, and
  `resolve_materialized_output_import_target`. The boundary-mapping
  collection/validation, swap resolution, wrapper generation, and
  strip survive as plan/emit functions.
- **Stage-coupling comments** whose constraints §2.6 dissolved.
- **docs/design.md**: "Pipeline trajectory" rewritten to describe the
  landed shape (and its remaining step: materialize-into-emit);
  "Vendor chunk swapping" updated for plan-time gating; the
  sanitization plan's Track C section tombstoned; CODE_REVIEW vendor
  entries deleted by the PRs that resolve them.
- **Kept deliberately**: `validate_emitted_exports` (tripwire, §3.3);
  the post-emission consumer scan until PR 6's evidence (§3.2); the
  strip sweep's local `analyze_chunk` (§3.1); the live-proxy dangling
  specifier contract.

## 8. Staged implementation PRs

Each independently green behind `e2e/vendor_swap_test.rs` (incl. the
#2062 and #2084 fixtures) plus the full e2e suite; explicit
old/new-coexistence states are part of the plan, not an accident.

| PR  | size | content                                                                                                                                                                                                                                                                   | intermediate state                                                                                                        |
| --- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 1   | M    | Pure refactor: extract shared validate/resolve into `vendor/validate.rs`; thread typed `ChunkId` through vendor passes; no behavior change                                                                                                                                | pipeline unchanged                                                                                                        |
| 2   | M    | `VendorResolutionPlan` computed once post-prepare (classification, boundary mappings, symbol tables, wrapper-shape + `default_export_aliases` validation, consumer-shape classification as data); existing stages consume the plan; `ResolutionManifest<R>` consolidation | old waves still run, fed by the plan; vendor validation failures move earlier (intentional, asserted by updated fixtures) |
| 3   | M    | `ImportTarget` + lowering consults the plan: boundary-renamed construction, partial-swap imports/body replacements for **materialized** module bodies; post-materialize dispatchers narrowed to non-materialized files; `references_rewritten` parity fixture             | two application paths coexist, partitioned by file class; post-hoc gates still on                                         |
| 4   | M    | Unified pass-through emission rewriter (canonicalize + boundary mapping + partial-swap directive surgery, incl. `import()` / `new Worker` / `new URL` sources); plan-time consumer gate switched on; delete stages 0, 1a, and 3a/3b's consumer side                       | post-emission consumer scan retained as differential tripwire; suppress-chunk byte-compat pinned by golden test           |
| 5   | M    | Full swap → emission-set exclusion (chunk removal deleted); strip → vendor emission (§2.5 composition); wrappers / facades / manifests written at emit; `swap_vendor_chunks` as a stage deleted                                                                           | pipeline has no vendor stages; reports derive from plan + emission results                                                |
| 6   | S    | Deletions (§7), retire the post-emission consumer scan with fixture evidence, re-point `validate_emitted_exports` at emission outputs, design.md / CODE_REVIEW / sanitization-plan doc sync, tombstone this plan                                                          | —                                                                                                                         |

Sequencing constraints: PRs 1–2 are prerequisites for everything; 3
and 4 can land in either order (they touch disjoint application
sites) but both before 5; 5 before 6. Track B (RenameLedger) is
complete, so the lowering side is stable; Track D's
`ChunkManifest`/facts work touches different files and can
parallelize.

## 9. Open questions

1. **`ImportTarget` placement.** §2.2 recommends a planner-boundary
   enum over a `ModuleId` variant. The alternative resurfaces if a
   future design wants the _gate_ to reason about external evaluation
   order (e.g. package side-effect ordering constraints). Nothing
   today needs that — externals are A7 leaves — so the recommendation
   stands; revisit only with a concrete cross-chunk ordering
   requirement.
2. **External import ordering inside module files.** §2.2 places
   package imports in the runtime re-import block (after sorted
   intra-chunk imports), matching today's post-hoc rewrite position.
   If a vendor package's init side effects must interleave _between_
   intra-chunk modules, this is wrong — but no such case exists in
   the corpora (vendor init is self-contained), and the entry file
   preserves source positions. Recommendation: accept; pin with the
   bundled-swap runtime fixture
   (`bundled_partial_swap_runtime_cannot_mix_swapped_client_with_residual_singleton_user`)
   which exercises real cross-module facade evaluation.
3. **Suppress-level byte-compat under emission-time canonicalization.**
   Today stage 0 canonicalizes _every_ AST file, including
   `suppress`-marked chunks — design.md's "byte-identical" wording for
   suppress is aspirational where rewritable specifiers exist. PR 4
   should pin current behavior with a golden test and decide whether
   suppress should _skip_ the directive rewriter entirely (making the
   documented contract true). Recommendation: skip it — suppress means
   "hands off", and the canonical form is only needed by files the
   pipeline rewrites for other reasons.
4. **Retiring the post-emission consumer scan.** §3.2's coverage
   argument is analytic; PR 6 retires the scan only with fixture
   evidence (every #2062 fixture failing at plan time with equivalent
   diagnostics). If any case is reachable post-emission but not at
   plan time, the scan stays and this doc's claim gets corrected.
5. **Strip's local re-analysis.** Could the self-rewrite be modeled as
   a fact _delta_ so strip consumes prepare-time facts? Rejected for
   now: the delta machinery would exist for one consumer over one
   file per swapped chunk, and `analyze_chunk` there is not a hot
   path. Recorded so the manifest-facts rule's boundary stays
   explicit.
6. **`references_rewritten` drift tolerance.** If plan-driven
   construction legitimately changes counts (e.g. import coalescing
   merges two directives the post-hoc rewriter counted separately),
   PR 3's parity fixture will surface it; maintainer call whether to
   chase exact parity or document the new basis in the report schema.
