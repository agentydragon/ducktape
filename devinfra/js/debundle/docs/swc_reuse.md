# SWC ecosystem reuse

What the debundler adopts from SWC, what it deliberately reimplements, and the
evaluations behind both — recorded so the same investigations are not re-run.

Currently pinned: `swc_common` 21.0.1, `swc_ecma_ast` 23.0.0, `swc_ecma_codegen` 26.0.1, `swc_ecma_parser` 39.0.2, `swc_ecma_utils` 29.1.1 (only `find_pat_ids` used), `swc_ecma_transforms_base` (resolver only), `swc_ecma_visit` 23.0.0, `swc_atoms` 9.0.0. SWC source cloned at `~/code/swc` for reference.

## `swc_ecma_utils` underutilized

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

## Rejected: `swc_ecma_transforms_optimization::simplify::dce` for the strip sweep

Replacing the vendor strip sweep (`sweep_unreachable_top_level` in `vendor/strip.rs`) with SWC's standalone DCE pass was evaluated and rejected as **unsound for this use case**. The strip sweep must delete _referenced, side-effectful_ swap-private statements — CJS module IIFEs, prototype wiring — that a conservative DCE retains precisely because they are referenced and side-effecting. Conversely, the sweep's split-brain and observable-effect gates (refusing to drop a statement still reachable from the residual chunk, or whose observable effect is not provably swap-private) are exactly the checks DCE lacks. Do not revisit without a design that covers both.

## Dead end: `swc_ecma_usage_analyzer`

No longer a standalone crate — absorbed into `swc_ecma_minifier` as `pub(crate)` module (`~/code/swc/crates/swc_ecma_minifier/src/usage_analyzer/mod.rs`). The "do not use directly" warning is **architectural**, not just semver: it depends on the minifier's internal `Marks` system (`const_ann`, `noinline`, `pure`, `fake_block`, `top_level_ctxt`, `unresolved_mark`) and a `Storage` trait requiring ~20 minifier-specific methods (`prevent_inline`, `mark_as_exported`, `mark_used_as_callee`, `store_param_count`, `add_infects_to`, etc.). Cannot be used outside `swc_ecma_minifier` without forking.

## Not worth replacing (domain-specific or unavailable)

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
