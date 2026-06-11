# Debundle Code Review

Full-package review of `devinfra/js/debundle/` (~47K lines). Findings prioritized by impact.

---

## P0 — God Modules

### `realizability/mod.rs` — remaining `gate_perf_counters` extraction

The simulator (`realizability/esm_simulator.rs`), the incremental quotient + overlay machinery (`realizability/incremental_quotient.rs`), and the test module (`realizability/tests.rs`) are split out. Remaining: `gate_perf_counters` (a ~490-line `pub mod` in `realizability/mod.rs`) → its own module. Caveat (re-confirmed 2026-06 during the split): the counters are entangled with index internals — `use super::*`, `pub(super)` recording APIs/statics called from inside `RealizabilityIndex` and `IncrementalQuotient` query methods, and shadow state (`IncrementalQuotient::base_snapshot_stale`) that exists only for the timing path. A clean move needs that coupling untangled first (e.g. a narrow recording trait), not just a file move.

### `vendor/mod.rs` further split

Remaining: strip-specific helpers and annotation/identity logic could each lift out into their own modules alongside the already-extracted `vendor/{manifests,strip,validate,wrappers}.rs`.

### `purity/mod.rs` (~2570 lines) — remaining concerns

Whitelist tables already in `purity/whitelists.rs`; long PlainData /
`PURE_OBJECT_CALLS_ON_PLAIN_DATA` rustdocs already trimmed to
`docs/purity_soundness.md`. Remaining: graph construction
(`ChunkCodeGraph`), expression classification, PlainData write
scanning, and TS enum IIFE recognition still live in `mod.rs` —
could be sub-split further if it keeps growing.

---

## P1 — Major Duplication

### Two parallel top-level fact extractors

`program_analysis.rs::analyze_program_shallow` (import/export records, owner records, side-effect ordinals, rewritable-specifier booleans) keeps its own top-level traversal and declaration classification (`classify_top_level_decl`) alongside the `facts/` walk the analysis pipeline uses. The two rule sets can drift independently; fold the shallow extractor into the facts traversal or derive its records from `StatementFacts`.

### Test fixture builders in peel/ remaining duplicates

`peel/test_utils.rs` already hosts `binding()`, `member()`, `module_ref()`. The `owner()`, `atomic_unit()`, `atomic_edge()`, `graph_fixture()` helpers have different signatures/semantics between the two test modules and remain local — could probably be unified with a small enum-tagged builder.

---

## P2 — Structural Issues

### `lowering/lower.rs` — monolithic function (partially extracted)

`lower_chunk` had 8 sequential phases inline. Four have been extracted into named functions (`compute_selected_ordinals`, `plan_selected_exports`, `split_entry_body`, `build_module_output`). Remaining inline phases (naturalization, disambiguation, import planning, the per-module loop) could be further extracted, though each requires substantial captured state from `LowerChunkInputs` (15–20 fields).

### `lowering/mod.rs` — ~95-line import block in a 295-line file

Consequence of wildcard `use super::*` in every sub-module. A more targeted import strategy would reduce this.

### `output_layout.rs` — 10 identical accessor methods

Each returns `self.root.join(CONSTANT)`. Replace with a data-driven approach: `report_path(name: &str) -> PathBuf` plus constants, or a const array + index.

---

## P3 — Encapsulation & Type Design

### `BTreeMap`/`BTreeSet` as default collection in hot-path graph structures

`rollback_graph.rs`, `artifact.rs`, `realizability/` all use BTree collections exclusively. For structures with many lookups, `HashMap`/`HashSet` would be faster. If deterministic iteration is needed, document it at the struct level. `RollbackDiGraph` in particular does many lookups per operation where hash-based would be measurably faster.

### `DepKind` 6-way split vs primary constraining/non-constraining axis

Callers in realizability/, validation.rs, facts/mod.rs frequently partition into constraining vs non-constraining via `constrains_init_order()`. Make this a first-class type distinction.

### Three-layer edge representation

Domain graph (`graph.rs`) → rollback graph (`rollback_graph.rs`) → realizability index (`realizability/`) all represent "edges between things" at different granularities with different semantics. The bridging code is fragile. Consider a unified edge model or explicit conversion layers.

### `pub(super)` everywhere in lowering/

Nearly every struct field and function is `pub(super)`. This is "module-private exposed to the entire parent module" — everything accessible everywhere within `lowering/`. Fields should be `pub(super)` only on structs that are actually constructed/destructured across file boundaries.

### Vendor manifest struct proliferation

~26 manifest/counts/detail structs with similar shapes in `vendor/manifests.rs`. `PartialSwapResolutionManifest` and `BundledPartialSwapResolutionManifest` are field-for-field twins — a generic `ResolutionManifest<R>` collapses the pair. `PartialSwapSymbolTarget` (`vendor/mod.rs`) is likewise a field-for-field twin of `spec::PartialSwapSymbol`.

### `SourceImportResolution = Option<(String, String, String)>` (`plan_references.rs:41`)

Unclear what the three strings mean. A named struct or comment would help.

---

## P5 — Test-Specific Issues

### Cycle-forcing fixture pattern (~20 repetitions across 4 files)

Every test in `purity_test.rs`, `object_plain_data_calls_test.rs`, `pure_members_test.rs`, and `at_init_s_chain_dataflow_test.rs` follows: create source with SE anchor + target binding + reader → run fixture → assert module source and entry output. A shared `assert_pure_cycle_break(source, logical_modules, module_path, contains, not_contains, expected_stdout)` in support.rs would eliminate ~300 lines.

### `accepted_spec_runs_under_node_test.rs` — `★ RED test` markers

Uses inline comment markers instead of `#[ignore]` with reason strings (like `purity_test.rs` does). Inconsistent.

### `logical_module_with_*` constructor sprawl in `e2e/support.rs`

Six `logical_module_with_*` variants (binding groups, comment, anon, anon-alpha, anon-alpha-wildcards, anon-comment) plus the base `logical_module` differ only in which optional pieces they populate. A small builder replaces the family.

### `assert_generated_module_after_entry_script` hardcodes the entry path

The `e2e/support.rs` helper asserts against a literal `./static/app/entry.js`, so fixtures built with `with_chunk_id` can't use it. Derive the expected specifier from the fixture's chunk id.

### Whitespace OR-chain assertions

`e2e/comma_list_owner_split_test.rs` asserts emitted-code shapes via chains like `contains("{ x, y }") || contains("{x, y}") || contains("{x,y}")`. Parse and compare AST (or normalize whitespace) instead of enumerating printer styles.

### Differential coverage gaps in the incremental-quotient tests

`peel/quotient_integration_test.rs` checks the incremental index against references that share too much code with the system under test: most verdict comparisons use the kernel's own `project_partition` output as the reference partition (blind to projection bugs), and only `replay_partition` rebuilds a quotient independently — and it compares only `cycle_set()`. Also missing: randomized merge/partition sequences (current corpora are fixed, ≤6 owners) and differential coverage of the gate-residual promotion transition.

### Only Lemma 2 has a named pinning test

docs/design.md proves Lemmas 1–5; only Lemma 2 appears in a test name (`e2e/lemma_two_rescued_asymmetric_cycle_test.rs`). Lemmas 1/3/4/5 — notably Lemma 4's lazy-read argument — have no named tripwire test that would fail if the proved property is weakened. Add one pinning e2e per lemma.

---

## Data Shape Smells

Open structural follow-ups from the pipeline data-shape audit.

### `ChunkBundle` ownership ping-pong

Ownership still passes through every stage via return: `artifact = result.artifact`. Could be cleaner with a builder or consuming pipeline, but each stage is now a pure function so the remaining smell is cosmetic.

---

## SWC Ecosystem — Reuse Opportunities

Currently pinned: `swc_common` 21.0.1, `swc_ecma_ast` 23.0.0, `swc_ecma_codegen` 26.0.1, `swc_ecma_parser` 39.0.2, `swc_ecma_utils` 29.1.1 (only `find_pat_ids` used), `swc_ecma_transforms_base` (resolver only), `swc_ecma_visit` 23.0.0, `swc_atoms` 9.0.0. SWC source cloned at `~/code/swc` for reference.

### `swc_ecma_utils` underutilized

The crate (at pinned 29.1.1) provides more than just `find_pat_ids`. Additional functions worth investigating:

| Utility                                   | Location in `swc_ecma_utils` | Debundle use case                                                                                                                                                                                                                                      |
| ----------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `IdentRenamer`                            | line 2377                    | Bulk `Id`→`Id` rename handling export specifiers, shorthand props, object patterns. More correct than custom `IdentifierRenamer` for hygiene-aware renames, but the debundler's string-keyed renames are post-hygiene so the abstraction doesn't match |
| `RefRewriter<T: QueryRef>`                | line 2494                    | Advanced reference rewriting — can replace an identifier with an arbitrary expression (e.g., `foo` → `bar.baz`). Could simplify cross-module import rewriting                                                                                          |
| `contains_ident_ref(ident, node)`         | line 126                     | Hygiene-aware "is this identifier referenced?" check. Could replace some manual visitor walks in fact collection                                                                                                                                       |
| `may_have_side_effects(ExprCtx)`          | trait method line 724        | Side-effect analysis on expressions. Could supplement purity classification for simple cases                                                                                                                                                           |
| `is_pure_callee(ExprCtx)`                 | trait method                 | Checks if calling an expression is safe. Could supplement purity classification                                                                                                                                                                        |
| `is_simple_pure_expr(expr, pure_getters)` | line 1260                    | Simple purity check. Could replace some inline purity checks in vendor stripping                                                                                                                                                                       |
| `replace_ident(node, from, to)`           | line 2070                    | Single-identifier replacement handling shorthand props. Lighter than full `IdentRenamer`                                                                                                                                                               |
| `collect_decls_with_ctxt`                 | line 2256                    | Like `collect_decls` but filters to a specific `SyntaxContext`. Could be useful for scope-aware binding collection                                                                                                                                     |

### `swc_ecma_transforms_optimization::simplify::dce` — evaluated and rejected

Replacing the vendor strip sweep (`sweep_unreachable_top_level` in `vendor/strip.rs`) with SWC's standalone DCE pass was evaluated and rejected as **unsound for this use case**. The strip sweep must delete _referenced, side-effectful_ swap-private statements — CJS module IIFEs, prototype wiring — that a conservative DCE retains precisely because they are referenced and side-effecting. Conversely, the sweep's split-brain and observable-effect gates (refusing to drop a statement still reachable from the residual chunk, or whose observable effect is not provably swap-private) are exactly the checks DCE lacks. Do not revisit without a design that covers both.

### `swc_ecma_usage_analyzer` — Dead end

No longer a standalone crate — absorbed into `swc_ecma_minifier` as `pub(crate)` module (`~/code/swc/crates/swc_ecma_minifier/src/usage_analyzer/mod.rs`). The "do not use directly" warning is **architectural**, not just semver: it depends on the minifier's internal `Marks` system (`const_ann`, `noinline`, `pure`, `fake_block`, `top_level_ctxt`, `unresolved_mark`) and a `Storage` trait requiring ~20 minifier-specific methods (`prevent_inline`, `mark_as_exported`, `mark_used_as_callee`, `store_param_count`, `add_infects_to`, etc.). Cannot be used outside `swc_ecma_minifier` without forking.

### Not worth replacing (domain-specific or unavailable)

| What                                        | SWC Equivalent                                                             | Why not                                                                                                                         |
| ------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Scope analysis (eager/lazy, TDZ, at-init)   | None available externally — usage_analyzer is `pub(crate)` in the minifier | No SWC crate models the eager/lazy distinction or call-promotion semantics                                                      |
| Purity analysis (call-graph SCC, PlainData) | None                                                                       | Entirely domain-specific to bundle deconstruction                                                                               |
| Realizability checking                      | None                                                                       | Incremental quotient maintenance is unique to this codebase                                                                     |
| Identifier renaming (flat string-keyed)     | `swc_ecma_utils::IdentRenamer` is `Id`→`Id`, not `String`→`String`         | Debundler needs flat textual rename on already-resolved hygiene contexts with string keys from spec YAML                        |
| `strip_parens`                              | None at pinned 29.1.1                                                      | Comment in source confirms: "swc's pinned swc_ecma_utils doesn't ship a stable equivalent"                                      |
| `SourceLineIndex`                           | `SourceMap::lookup_char_pos` available at 21.0.1                           | Local version is a legitimate perf optimization (pre-computed binary search); could simplify to direct calls if perf acceptable |
| `member_root_id`/`member_root_sym`          | None                                                                       | No SWC utility for extracting root of member expression chains                                                                  |
| Import declaration construction             | `ExprFactory` trait (partial) — individual node construction only          | Debundle-specific relative-path logic has no SWC equivalent; keep but consolidate 3 copies                                      |

---

## Top Highest-Impact Actions

1. **Extract `gate_perf_counters` from `realizability/mod.rs`** — see the P0 item; needs the recording-API entanglement untangled first.

2. **Continue splitting `vendor/mod.rs`** — manifests, strip, wrappers, and validate/resolve helpers extracted; remaining: strip-specific helpers, annotation/identity logic.

3. **Consolidate the data-shape follow-ups** — remove the remaining `ChunkBundle` ownership ping-pong.
