# F5 feasibility: `source_match_body_debt` near-miss diagnostics on the fact model

Investigation for the F5 (full `AstWildcardMatcher` deletion) decision. Scope:
**only** the `source_match_body_debt` near-miss diagnostics surface. (Sibling
agents cover `member_candidates` and the codemod minimizer.)

Question: can the near-miss diagnostics be reproduced **faithfully** from the
fact model `ChunkResolver`/`datalog_resolver.rs` already operates on, or do they
fundamentally require `AstWildcardMatcher`'s AST-walk structure?

## VERDICT: GO

Every near-miss variant is reproducible on the fact model. The reason text is
"AST-walk-shaped" only in the sense that it names node kinds, labels, and child
positions — **all of which the fact EDB already carries node-for-node**, and all
of which the fact matcher's own recursive descent (`homo`/`match_children`/
`align_var_declarators`) already visits in the same order. The fact matcher today
_discards_ the divergence location by collapsing to `Result<bool>`; the near-miss
path is exactly that same descent instrumented to return _where and why_ the
first `false` happened. No positional/structural state is missing.

This is a **real engineering task** (re-implement the scored "first divergence"
walk against `selector_match::Index` instead of `swc_ecma_ast`), not a dead end.
The runbook's pessimistic framing ("the reason text is inherently AST-shaped",
plan line 158-163, 195) is based on the reason _strings_ looking AST-shaped — but
the facts ARE a faithful AST projection, so that shape is fully available.

Because resolution is already fully served by facts (F4 landed), this surface
does **not block** the headline goal. It is the last thing standing between
"sole resolver" and "literal deletion of `AstWildcardMatcher`".

## What the surface is

`source_match_body_debt` (`source_match/binding_resolution.rs:321-394`) returns
`SourceMatchBodyDebt { exact_groups, near_misses }` (`source_match/types.rs:9-21`).

Two consumers, both rendering the SAME data:

1. **Lowering failure reporting** — `lowering/materialize/plan_builder.rs:187-211`
   (`source_match_report_details`) calls `source_match_body_debt(.., 1, 3)` and
   maps each near-miss into `SelectorNearestCandidate { body_index,
declared_bindings, score, first_mismatch: reason }`. Shown when a `source_match`
   fails to resolve during a real run.
2. **Spec-validate `--source-aware`** — `selector_debt.rs:995-1029`
   (`collect_source_aware_debt`) → `SourceAwareStructuralSelector.near_misses`,
   rendered verbatim at `selector_debt.rs:1110-1120` as
   `score={} body={} declared=[{}] {reason}`.

`source_match_body_debt` itself splits into two independently-sourced halves:

- **`exact_groups: Vec<Vec<Option<usize>>>`** — from
  `find_matching_body_group_alignments` (`source_match/body_search.rs:103-144`),
  the exact-matcher alignment.
- **`near_misses: Vec<SourceMatchNearMiss>`** — the scored "first structural
  divergence" rows, one per non-matching top-level candidate scoring `>= min_score`
  (`binding_resolution.rs:354-389`).

`SourceMatchNearMiss { body_idx, declared_bindings, score, reason }`
(`types.rs:9-15`).

## Fact-model status of each piece

### `exact_groups` — status (a): already in facts

`find_matching_body_group_alignments` returns `Vec<Vec<Option<usize>>>`. The fact
matcher already produces the **identical shape** via
`selector_match::match_top_level_sequence_indexed`
(`selector_match.rs:1162-1206`), which `ChunkResolver::resolve_anonymous_groups`
already uses in production. (Runbook line 159 already concedes this.) Single-item
needle: trivial one-element alignment from `matching_body_indices`
(`datalog_resolver.rs:200-232`). No new facts.

### `declared_bindings` per candidate — status (a): already available

`declared_bindings` (`source_match/declared_bindings.rs`) is a **pure AST helper**
over the candidate `ModuleItem` — no matcher involvement. `datalog_resolver.rs`
already calls it on matched items. A near-miss candidate is just another top-level
body item, so the same call works. (If a pure-facts implementation is wanted for
deletion symmetry, `node_kind` + `ident_name`/`child` carry every binding name —
but reusing the AST helper is fine; it is not `AstWildcardMatcher`.)

### `near_misses` reasons — status (a)/(b): present in facts; needs a new fact-walk function (not new facts)

This is the crux. Below is **every** reason variant the near-miss path can emit
(`hints.rs`), the identifying detail it carries, and the fact-model status.

The entry point is `first_mismatch_reason(needle, candidate, ...)`
(`hints.rs:726-753`): it **first runs `AstWildcardMatcher::match_module_item` to
confirm non-match**, then dispatches by `(needle, candidate)` shape. The fact
matcher's `selector_match::matches_indexed` already gives the boolean; the
dispatch below is a parallel descent that stops at the first divergence.

Crucially, the fact matcher's `homo` (`selector_match.rs:496-584`) already
encodes the SAME divergence order: (1) kind mismatch (line 529), (2) non-ident
label mismatch — str/num/bool/prop_name/operator/regex (lines 532-539), (3) ident
mismatch (line 550), (4) `super_class` mismatch (564-572), (5) children mismatch
(`match_children` 589-625, `align_var_declarators` 767-827). Each early `Ok(false)`
is one reason variant. A fact-based `first_mismatch_reason` instruments these
return points.

#### Per-variant table

| #   | Variant (constructing site)                                                                                 | Score | Identifying detail                                                                                                                                | Info needed                                                                            | Fact status                                                                                                                                                                  |
| --- | ----------------------------------------------------------------------------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | top-level item kind differs (`hints.rs:744-751`)                                                            | 1     | "selector is {statement/module declaration}, candidate is {...}"                                                                                  | top-level node kind of each                                                            | (a) `node_kind`                                                                                                                                                              |
| 2   | module decl kind differs (`hints.rs:772-781`)                                                               | 10    | "module declaration kind differs: selector is {import/export decl/...}, candidate is {...}"                                                       | `ModuleDecl` discriminant                                                              | (a) `node_kind` tags (`Import`/`ExportDecl`/`ExportNamed`/`ExportDefault`/`ExportDefaultDecl`/`ExportAll`)                                                                   |
| 3   | module decl shape differs (`hints.rs:782-785`)                                                              | 20    | "module declaration shape differs" (fixed)                                                                                                        | (catch-all)                                                                            | (a) trivial                                                                                                                                                                  |
| 4   | statement kind differs (`hints.rs:800-808`)                                                                 | 10    | "statement kind differs: selector is {if/while/return/...}, candidate is {...}"                                                                   | `Stmt` discriminant                                                                    | (a) `node_kind` (`If`/`While`/`Return`/`Throw`/`Try`/`Switch`/`For`/`ForIn`/`ForOf`/`ExprStmt`/`Block`/...)                                                                  |
| 5   | statement shape differs (`hints.rs:810-813`)                                                                | 20    | "statement shape differs" (fixed)                                                                                                                 | (catch-all)                                                                            | (a) trivial                                                                                                                                                                  |
| 6   | declaration kind differs (`hints.rs:861-869`)                                                               | 30    | "declaration kind differs: selector is {class/function/variable}, candidate is {...}"                                                             | `Decl` discriminant                                                                    | (a) `node_kind` (`ClassDecl`/`FnDecl`/`VarDecl`)                                                                                                                             |
| 7   | declaration shape differs (`hints.rs:871-874`)                                                              | 35    | "declaration shape differs" (fixed)                                                                                                               | (catch-all)                                                                            | (a) trivial                                                                                                                                                                  |
| 8   | class name differs (`hints.rs:826-833`, exact mode only)                                                    | 40    | "class name differs: selector \`X\`, candidate \`Y\`"                                                                                             | both class names                                                                       | (a) `ident_name` of the `ClassDecl` name child (ordinal 0)                                                                                                                   |
| 9   | function name differs (`hints.rs:844-851`, exact mode only)                                                 | 40    | "function name differs: selector \`X\`, candidate \`Y\`"                                                                                          | both fn names                                                                          | (a) `ident_name` of `FnDecl` name child (ordinal 0)                                                                                                                          |
| 10  | function signature/body differs (`hints.rs:853-856`)                                                        | 35    | "function signature or body differs" (fixed)                                                                                                      | (name matched, deeper differs)                                                         | (a) trivial (label only)                                                                                                                                                     |
| 11  | var decl keyword differs (`hints.rs:885-893`)                                                               | 45    | "variable declaration kind differs: selector is {var/let/const}, candidate is {...}"                                                              | both kinds                                                                             | (a) `operator` fact on `VarDecl` (`chunk_facts.rs:376-381`)                                                                                                                  |
| 12  | var declarators count differ, no hole (`hints.rs:912-918`)                                                  | 55    | "variable declarators differ: selector has N declarator(s), candidate has M"                                                                      | both declarator counts                                                                 | (a) `child` arity of the two `VarDecl` nodes                                                                                                                                 |
| 13  | var declaration shape differs (`hints.rs:921-924`)                                                          | 35    | "variable declaration shape differs" (fixed)                                                                                                      | (catch-all)                                                                            | (a) trivial                                                                                                                                                                  |
| 14  | class member matched-by-name, body differs (`hints.rs:952-958` via `first_class_mismatch_reason`)           | 65    | "class member \`L\` matched by name, but its signature or body differs"                                                                           | the member label `L` that matched by name but failed structurally                      | (a) `prop_name` on the member's key child; in-order label scan = the same scan over `child` ordinals + `prop_name`/`node_kind`                                               |
| 15  | class pinned member not found in order (`hints.rs:962-968`)                                                 | 70    | "selector class pinned member \`L\` was not found in the candidate class body in order"                                                           | the missing label `L`                                                                  | (a) `prop_name`/member `node_kind` (constructor/method/prop/static block) over the class `child` list                                                                        |
| 16  | class heritage/decorators/member-order differs (`hints.rs:971-974`)                                         | 45    | "class heritage, decorators, or member order differs" (fixed)                                                                                     | (catch-all)                                                                            | (a) trivial — but see note on **decorators** below                                                                                                                           |
| 17  | pinned declarator not found in order (`hints.rs:586-609` via `first_pinned_var_declarator_mismatch_reason`) | 55    | "selector pinned declarator #i \`label\` was not found in order (remaining candidate declarators: ...)"; labels via `render_var_declarator_label` | needle pinned declarator index + rendered label; remaining candidate declarator labels | (a)/(b) the alignment is `align_var_declarators`'s output (already computed); labels = `binding_targets::binding_name_strings` + `expr_shape_label` over the declarator init |
| 18  | leading unmatched declarators before first pin (`hints.rs:622-635`)                                         | 55    | "candidate has unmatched leading declarator(s) before selector declarator #i \`label\`: {skipped labels}. Add a DECLARATORS\_\* = null ..."       | first-pin alignment + skipped candidate declarator labels                              | (a)/(b) alignment + label rendering (see #17)                                                                                                                                |
| 19  | unmatched declarators between pins (`hints.rs:638-663`)                                                     | 55    | "candidate has unmatched declarator(s) between selector declarator #i \`l1\` and #j \`l2\`: {skipped}. Add a DECLARATORS\_\* ..."                 | inter-pin alignment gap + skipped labels                                               | (a)/(b) alignment + labels                                                                                                                                                   |
| 20  | trailing unmatched declarators after last pin (`hints.rs:669-685`)                                          | 55    | "candidate has unmatched trailing declarator(s) after selector declarator #i \`label\`: {skipped}. Add a DECLARATORS\_\* ..."                     | last-pin alignment + trailing labels                                                   | (a)/(b) alignment + labels                                                                                                                                                   |
| 21  | declarators matched, hole placement differs (fallback) (`hints.rs:619-620, 666-690`)                        | 55    | "pinned declarators matched in order, but DECLARATORS\_\* hole placement differed. ..." (fixed)                                                   | (catch-all after alignment)                                                            | (a) trivial                                                                                                                                                                  |

Plus the richer **multi-candidate hint** renderers (`source_match_no_match_hint`,
`class_source_match_no_match_hint`, `var_declarator_source_match_no_match_hint`,
`hints.rs:28-273`) — these are used by a _different_ entry point
(`source_match_no_match_hint`, not `source_match_body_debt`). They reuse the same
`first_*_mismatch_reason` primitives plus `count_pinned_class_member_labels_in_order`
/ `count_pinned_var_declarators_in_order` and label renderers. **Strictly out of
the `body_debt` scope as written, but they share 100% of the primitives below**, so
reproducing the `body_debt` reasons reproduces these for free. (Flagged because if
the goal is literal deletion of `AstWildcardMatcher`, `source_match_no_match_hint`
is another caller of these primitives — see "Other callers" below.)

#### Why all of this is fact-derivable

The reason strings cite exactly five categories of datum, every one of which is an
EDB relation:

- **Node kind** ("statement is `if`", "declaration is `class`") → `node_kind`
  (`chunk_facts.rs:40`). The kind tags are even finer than the AST-walk's
  `*_kind` helpers (e.g. `AsyncFunction` vs `Function`, `chunk_facts.rs:419-425`).
- **Identifier / member / declaration-keyword labels** ("class name `X`", "member
  `important`", "kind `const`") → `ident_name` (`:50`), `prop_name` (`:52`),
  `operator` (`:56`).
- **Child arity & order** ("N declarators", "found in order") → `child` carries
  `(parent, ordinal, child)` (`:43`), a faithful ordered tree.
- **String/num/bool/regex literals** → `str_lit`/`num_lit`/`bool_lit`/`regex`
  (`:45-58`).
- **`extends` / superclass** → `super_class` relation (`:60`).

The var-declarator alignment that variants 17-21 introspect is **already computed
by the fact matcher**: `align_var_declarators` (`selector_match.rs:767-827`) and
`place_declarator_segments` (`:829-907`) produce the exact `Vec<Option<usize>>`
greedy-leftmost alignment that `pinned_var_declarator_matches_in_order`
(`hints.rs:530-565`) recomputes on the AST side. So the "which pin failed / which
candidate declarators were skipped" logic is a read of an alignment the fact side
already produces — `align_var_declarators` even mirrors
`matcher::place_var_declarator_segments` line-for-line (its own doc-comment says
so, `selector_match.rs:825-827`).

The class member in-order scan (variants 14-15) is likewise a walk over the class
node's `child` list comparing `prop_name`/member-`node_kind` — the same scan
`first_class_mismatch_reason` (`hints.rs:934-970`) does over `class.body`, and the
same scan `count_pinned_class_member_labels_in_order` (`:488-517`) does.

### The scoring scheme — status (a): pure function of the divergence point

`score` is a fixed integer per divergence variant (1/10/20/30/35/40/45/55/65/70),
assigned at the point the AST walk decides the reason (the table above). It is a
ranking heuristic ("higher = closer"), not derived from AST node identity. A
fact-based walk that reaches the same divergence point assigns the same constant.
`min_score` filtering (`binding_resolution.rs:365`) and the `(score desc, body_idx
asc)` sort (`:381-386`) are then pure post-processing on the rows.

## The one genuine caveat (does NOT change the verdict)

**Decorators.** `chunk_facts.rs` does not project class/member decorators (no
decorator node kind in `class_member`, `chunk_facts.rs:461-513`; the corpus has
none, hence 100% coverage). Variant 16 ("class heritage, **decorators**, or member
order differs", score 45) names decorators in a **fixed catch-all string** — it
does not inspect decorator contents, it just lists the possible culprits. So:

- The fact walk reproduces variant 16's **exact string** trivially (it is a
  constant). No decorator facts needed for the _diagnostic_.
- BUT: if a future bundle had decorators, the fact **matcher** would fail closed
  (`Unsupported`) on the decorated node before reaching the near-miss path at all
  — `chunk_facts` extraction would `Err`. This is the existing fail-closed contract
  (`chunk_facts.rs:1-20`), not a near-miss regression. The AST matcher would
  instead silently compare them. This is a pre-existing matcher-coverage gap, the
  same one F3/F4 already gated on (corpus = 0 disagreements), **orthogonal to the
  diagnostics question.** It is already true for resolution today.

No other variant inspects any datum absent from the EDB.

## Cost estimate

- **New facts/relations: none.** Status (a) for every datum; the alignment for
  17-21 is status (a) (already computed by `align_var_declarators`).
- **New code:** a `fact_first_mismatch_reason(needle_index, subject_index, mode)
-> Option<(score, String)>` in `datalog_resolver.rs` (or a new
  `source_match/fact_hints.rs`), mirroring the 21 return points of `homo`/
  `match_children`/`align_var_declarators`. Plus the label renderers
  (`render_var_declarator_label`, `expr_shape_label`, `class_member_label`,
  `*_kind`) re-expressed over `Index` node reads. Estimate: comparable in size to
  `hints.rs` (~1000 lines), because it is essentially `hints.rs` retargeted from
  `swc_ecma_ast` to `selector_match::Index`. Mechanical, not novel.
- **Parity gate:** add a **near-miss differential** to `corpus_match_differential`
  — for every selector that fails to resolve over the real corpus, assert the
  fact-based `near_misses` rows (body_idx, score, reason, declared_bindings) equal
  the `AstWildcardMatcher` rows. This is the same differential discipline the plan
  already uses for the categorical verdict and the (planned) candidate-list. Until
  that differential is 0, the residual stands. This is the real work — the strings
  must match byte-for-byte to count as "faithful", and there are 21 templated
  variants.

## Recommendation

GO, with the explicit note that this is the **largest** of the three F5 residual
surfaces by code volume (it re-expresses all of `hints.rs` over facts) and the
strings must be reproduced verbatim under a new differential gate. There is no
fact-model blocker: every reason datum is in the EDB and every divergence point is
already a return site of the fact matcher's own descent. This does **not** force
the diagnostics-only-residual fallback. The fallback is only warranted if the
_effort_ of the verbatim-string differential is judged not worth it versus keeping
a small AST-walk helper alive — that is a cost/benefit call for the user, **not** a
faithful-encoding dead end.

### Other callers to fold in for literal deletion (scope-adjacent flag)

`first_mismatch_reason` and the `first_*_mismatch_reason` family are **also** reached
by `source_match_no_match_hint` (`hints.rs:28-273`), which is a separate
diagnostic entry point from `source_match_body_debt`. Anything that deletes
`AstWildcardMatcher` must reproduce or retire that caller too. It shares all the
primitives analyzed here, so it is GO on the same grounds — but it is a second
call site the deletion must account for. (Verify its consumers before deleting;
out of this investigation's strict `body_debt` scope.)
