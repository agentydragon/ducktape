# SWC ecosystem reuse

What the debundler adopts from SWC, what it deliberately reimplements, and the
evaluations behind both — recorded so the same investigations are not re-run.

## Rejected: `swc_ecma_transforms_optimization::simplify::dce` for the strip sweep

Replacing the vendor strip sweep (`sweep_unreachable_top_level` in `vendor/strip.rs`) with SWC's standalone DCE pass was evaluated and rejected as **unsound for this use case**. The strip sweep must delete _referenced, side-effectful_ swap-private statements — CJS module IIFEs, prototype wiring — that a conservative DCE retains precisely because they are referenced and side-effecting. Conversely, the sweep's split-brain and observable-effect gates (refusing to drop a statement still reachable from the residual chunk, or whose observable effect is not provably swap-private) are exactly the checks DCE lacks. Do not revisit without a design that covers both.

## Dead end: `swc_ecma_usage_analyzer`

No longer a standalone crate — absorbed into `swc_ecma_minifier` as a `pub(crate)` module. The "do not use directly" warning is **architectural**, not just semver: it depends on the minifier's internal `Marks` system (`const_ann`, `noinline`, `pure`, `fake_block`, `top_level_ctxt`, `unresolved_mark`) and a `Storage` trait requiring ~20 minifier-specific methods (`prevent_inline`, `mark_as_exported`, `mark_used_as_callee`, `store_param_count`, `add_infects_to`, etc.). Cannot be used outside `swc_ecma_minifier` without forking.

## Not worth replacing (domain-specific or unavailable)

| What                                        | SWC Equivalent                                                             | Why not                                                                                                  |
| ------------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Scope analysis (eager/lazy, TDZ, at-init)   | None available externally — usage_analyzer is `pub(crate)` in the minifier | No SWC crate models the eager/lazy distinction or call-promotion semantics                               |
| Purity analysis (call-graph SCC, PlainData) | None                                                                       | Entirely domain-specific to bundle deconstruction                                                        |
| Realizability checking                      | None                                                                       | Incremental quotient maintenance is unique to this codebase                                              |
| Identifier renaming (flat string-keyed)     | `swc_ecma_utils::IdentRenamer` is `Id`→`Id`, not `String`→`String`         | Debundler needs flat textual rename on already-resolved hygiene contexts with string keys from spec YAML |
| `SourceLineIndex`                           | `SourceMap::lookup_char_pos`                                               | Local version precomputes per-file line starts and binary-searches them: a perf optimization             |
| `member_root_id`/`member_root_sym`          | None                                                                       | No SWC utility for extracting root of member expression chains                                           |
| Import declaration construction             | `ExprFactory` trait (partial) — individual node construction only          | Debundle-specific relative-path logic has no SWC equivalent                                              |

## Replaceable, not yet migrated

`binding_targets::strip_parens` duplicates `swc_ecma_ast::Expr::unwrap_parens`
(same semantics, verified in `swc_ecma_ast` 29.0.1). `swc_ecma_utils` itself has
no equivalent.
