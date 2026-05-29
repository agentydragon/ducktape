# Debundle Code Review

Full-package review of `devinfra/js/debundle/` (~47K lines). Findings prioritized by impact.

---

## P0 — God Modules

### `vendor/mod.rs` further split

Remaining: strip-specific helpers, annotation/identity logic, wrapper generation could each lift out into their own modules alongside the already-extracted `vendor/manifests.rs` and `vendor/strip.rs`.

### `purity/mod.rs` (~2300 lines) — remaining concerns

Whitelist tables already in `purity/whitelists.rs`; long PlainData /
`PURE_OBJECT_CALLS_ON_PLAIN_DATA` rustdocs already trimmed to
`docs/purity_soundness.md`. Remaining: graph construction
(`ChunkCodeGraph`), expression classification, PlainData write
scanning, and TS enum IIFE recognition still live in `mod.rs` —
could be sub-split further if it keeps growing.

### `facts/mod.rs` (1213 lines, 2 concerns)

`StatementFacts` carries many derivable `BTreeSet<Id>` sets that every construction site must keep mutually consistent — see the canonical "### `StatementFacts`" item under P3.

---

## P1 — Major Duplication

### Test fixture builders in peel/ remaining duplicates

`peel/test_utils.rs` already hosts `binding()`, `member()`, `module_ref()`. The `owner()`, `atomic_unit()`, `atomic_edge()`, `graph_fixture()` helpers have different signatures/semantics between the two test modules and remain local — could probably be unified with a small enum-tagged builder.

---

## P2 — Structural Issues

### `lowering/lower.rs` — monolithic function (partially extracted)

`lower_chunk` had 8 sequential phases inline. Four have been extracted into named functions (`compute_selected_ordinals`, `plan_selected_exports`, `split_entry_body`, `build_module_output`). Remaining inline phases (naturalization, disambiguation, import planning, the per-module loop) could be further extracted, though each requires substantial captured state from `LowerChunkInputs` (15–20 fields).

### `lowering/mod.rs` — 266-line import block

Consequence of wildcard `use super::*` in every sub-module. A more targeted import strategy would reduce this.

### `output_layout.rs` — 10 identical accessor methods

Each returns `self.root.join(CONSTANT)`. Replace with a data-driven approach: `report_path(name: &str) -> PathBuf` plus constants, or a const array + index.

---

## P3 — Encapsulation & Type Design

### `BTreeMap`/`BTreeSet` as default collection in hot-path graph structures

`rollback_graph.rs`, `artifact.rs`, `realizability.rs` all use BTree collections exclusively. For structures with many lookups, `HashMap`/`HashSet` would be faster. If deterministic iteration is needed, document it at the struct level. `RollbackDiGraph` in particular does many lookups per operation where hash-based would be measurably faster.

### `StatementFacts` (facts/mod.rs) — ~10 BTreeSet<Id> fields

`declared`, `eager_reads`, `eager_rebinds`, `lazy_reads`, `lazy_rebinds`, `first_order_lazy_reads`, `first_order_lazy_rebinds`, `local_effects`, `at_init_calls`, `body_calls`, `first_order_body_calls`. Several are derivable (e.g. the `first_order_*` sets are subsets of their parent). Every construction site must keep the sets mutually consistent. Consider computing derived sets on demand or using a builder that enforces invariants.

### `DepKind` 6-way split vs primary constraining/non-constraining axis

Callers in realizability.rs, validation.rs, facts.rs frequently partition into constraining vs non-constraining via `constrains_init_order()`. Make this a first-class type distinction.

### Three-layer edge representation

Domain graph (`graph.rs`) → rollback graph (`rollback_graph.rs`) → realizability index (`realizability.rs`) all represent "edges between things" at different granularities with different semantics. The bridging code is fragile. Consider a unified edge model or explicit conversion layers.

### `pub(super)` everywhere in lowering/

Nearly every struct field and function is `pub(super)`. This is "module-private exposed to the entire parent module" — everything accessible everywhere within `lowering/`. Fields should be `pub(super)` only on structs that are actually constructed/destructured across file boundaries.

### Vendor manifest struct proliferation

~15 manifest/counts/detail structs with similar shapes in `vendor.rs`. `PartialSwapResolutionManifest` and `BundledPartialSwapResolutionManifest` are nearly structurally identical. Consider a parameterized base type.

### `SourceImportResolution = Option<(String, String, String)>` (`plan_references.rs:41`)

Unclear what the three strings mean. A named struct or comment would help.

---

## P5 — Test-Specific Issues

### Cycle-forcing fixture pattern (~20 repetitions across 4 files)

Every test in `purity_test.rs`, `object_plain_data_calls_test.rs`, `pure_members_test.rs`, and `at_init_s_chain_dataflow_test.rs` follows: create source with SE anchor + target binding + reader → run fixture → assert module source and entry output. A shared `assert_pure_cycle_break(source, logical_modules, module_path, contains, not_contains, expected_stdout)` in support.rs would eliminate ~300 lines.

### `NodeOutput` struct is dead (`support.rs:484`)

Identical to `CommandResult` (line 660). Only used in `assert_node_output` which internally converts to `CommandResult`. Remove and use `CommandResult` directly.

### `accepted_spec_runs_under_node_test.rs` — `★ RED test` markers

Uses inline comment markers instead of `#[ignore]` with reason strings (like `purity_test.rs` does). Inconsistent.

---

## Data Shape Smells

Open structural follow-ups from the pipeline data-shape audit.

### `ChunkBundle` ownership ping-pong

Ownership still passes through every stage via return: `artifact = result.artifact`. Could be cleaner with a builder or consuming pipeline, but each stage is now a pure function so the remaining smell is cosmetic.

### Pipeline ordering — `generated_by_selected_module_lowering` flag

`generated_by_selected_module_lowering` exists solely so `rewrite_chunk_entry_specifiers` can skip specifier rewriting on files synthesized by the lowering stage. This flag wouldn't be needed if specifier rewriting ran _before_ lowering. Investigate whether reordering the pipeline stages eliminates the need for the flag entirely.

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

### `swc_ecma_transforms_optimization::simplify::dce` — Potential DCE replacement

The DCE pass that `swc_ecma_minifier` uses actually lives in `swc_ecma_transforms_optimization` (a separate, lighter crate). It is **public and standalone-usable**:

```rust
use swc_ecma_transforms_optimization::simplify::dce;

let mut shaker = dce(
    dce::Config {
        module_mark: None,
        top_level: true,
        top_retain: vec!["keep_me".into()],
        preserve_imports_with_side_effects: true,
    },
    unresolved_mark,
);
module.visit_mut_with(&mut shaker);
```

**Algorithm**: Two-phase fixed-point — `Analyzer` builds a `petgraph` dependency graph of variable references, tracks entry points, subtracts SCC-internal usage via Tarjan, then `TreeShaker` removes zero-usage bindings. Handles eval/arguments conservatively, self-references in fn/class bodies, IIFE unfolding.

**Adaptation path for partial-swap**: The DCE removes what's _unreferenced_. For "strip these specific exports":

1. Preparatory pass removes target export specifiers from the module
2. Run DCE with `top_level = true` and `top_retain` listing the residual symbols to keep
3. DCE transitively drops bindings that only the removed exports used

This would replace the custom `sweep_unreachable_top_level` in `strip_swapped_vendor_exports.rs` (~300 lines). The debundle's split-brain detection (checking that dropped items aren't read by kept items) would still need a custom validation pass after DCE runs. Worth investigating whether the DCE's own `can_drop_binding` logic covers this or whether a post-DCE validation scan is sufficient.

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

## Top 5 Highest-Impact Actions

1. **Continue splitting `vendor/mod.rs`** — manifests, strip, and partial-swap dispatchers extracted; remaining: strip-specific helpers, annotation/identity logic, wrapper generation.

2. **Split `analysis_tests.rs`** into 6–8 topic-aligned test modules. Largest test file at 4095 lines.

3. **Simplify `StatementFacts`** (`facts/mod.rs`) — see the canonical "### `StatementFacts`" item under P3.

4. **Consolidate the data-shape follow-ups** — remove the remaining `ChunkBundle` ownership ping-pong and the `generated_by_selected_module_lowering` ordering workaround if stage reordering makes that flag unnecessary.
