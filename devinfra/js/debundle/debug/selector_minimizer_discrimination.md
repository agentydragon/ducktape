# Recursive selector minimizer: discrimination-driven anchor selection

Status: in progress (started 2026-06-15). Tracks the work behind the ignored
`//devinfra/js/debundle/e2e:selector_minimizer_expectation_test` suite.

## Goal

`debundle spec synthesize-selectors` should, for a target binding living among
near-duplicate siblings in a chunk, emit the **sparsest robust** `source_match`
selector — keeping the meaningful, stable anchors that identify the target and
holing the incidental/volatile rest (`ANYTHING`, `STMT_LIST`, `OBJECT_PROPS`,
`CLASS_REST`, `DECLARATORS_*`), interleaving holes and partial statements at
every nesting level.

Perf budget: ideally <10s per invocation, 60s hard.

### Motivation (why minimal selectors matter to a debundle user)

The spec is re-applied on every `debundle run` against a blob whose minified
identifiers churn between rebuilds and version ports. A `selector.binding.name`
pin is therefore rebuild-fragile (the name re-mints and the RE work silently
detaches — this is the "selector debt" `selector-debt` measures). A
`source_match` selector anchors on what the code _is_ (shape, literals, calls,
config keys), which survives re-minification. The minimizer turns a brittle
exact body into the concise anchor that re-identifies the entity across future
builds while staying readable in the spec.

### Two gates (from `skills/shared/workflow.md`)

1. **Uniqueness/correctness** — must match and claim the intended current entity.
2. **Conciseness/robustness** — must not overpin incidental bodies, arguments,
   generated values, or unrelated siblings.

Exact long bodies satisfy gate 1 only; they are drafts to minimize.

### Objective is dual, not pure discrimination

Keep anchors that are _meaningful, stable landmarks_ (distinctive string
literals, API method calls, config `key: value`s) — these both discriminate
from siblings AND durably re-identify the entity in future builds, so retaining
one is worthwhile even when it is not the minimum needed to beat today's
siblings. Hole what is incidental/volatile (transient locals, generated values,
ordering noise, unrelated siblings). The cost model should _prefer retaining
concrete meaningful content over incidental structure_, not merely minimize
token count.

### Selector groups are a first-class minimization target

A **binding group** is one `source_match` + `exports:` map that resolves
several members at once (spec `binding_groups:`; matcher support already exists
via `resolve_member_binding_group_match`). Preferring a group over N individual
selectors **avoids multiplication**: the shared enclosing structure is described
once instead of N times, and the cluster re-identifies as a unit (fewer,
sturdier anchors to maintain across rebuilds).

Minimizing a group is the same anchor-cover search, but the uniqueness target is
the _tuple_ of target slots resolving to the right exports. It must (1) hole all
non-target slots/structure (`DECLARATORS_BETWEEN/_AFTER`, `OBJECT_PROPS`, ...),
(2) keep enough shared anchors to claim the enclosing declaration uniquely among
chunk siblings, and (3) keep enough per-member anchors to bind each export to
the correct slot when members are otherwise alike (a literal like `"primary"`
vs `"secondary"` can both claim the group and disambiguate members). The
existing `VarSlotConstraintSearch` already does this for declarator groups
(`binding_group_declarators` passes).

The genuinely new piece is the **grouping decision** (`binding_group_partition`):
partition requested targets into groups-vs-standalone — group those sharing
structure, split off distant ones — so the minimizer emits one group + one
standalone rather than three individual selectors. This is part of the
objective, not a post-hoc step.

## Why the PR 2250 implementation produces over-/mis-pinned selectors

Running the suite with `--ignored`: 1/7 pass (`binding_group_declarators`, the
var-group path). The other 6 fail with two root causes:

1. **Objective is structural, not discriminative.** Candidates are ranked by
   `(cost, source.len())` where a _kept but fully holed_ statement
   (`const X = ANYTHING`, cost 1) is cheaper than dropping it into `STMT_LIST`.
   So the search finds a selector that is unique only by _accidental position_
   (e.g. "target is the only function starting with two `const` decls") instead
   of keeping the literal/call that genuinely differs from siblings.
   Example — `sparse_function_body`:
   - got: `function F(A,A,A){ const transient=ANYTHING; const marker=ANYTHING; STMT_LIST }`
   - want: `function F(A,A,A){ STMT_LIST; const marker=123; STMT_LIST; A.foo(A,123); STMT_LIST }`

2. **Renderers cannot express multiple anchors within one node.**
   `render_object_expr_selector_variants` only ever retains ONE object key
   (`{kind:"primary", OBJECT_PROPS}`); `object_property_literals` needs two
   (`kind` + `mode`). The class path anchors on member _names_ only and renders
   member bodies as bare `STMT_LIST`; `class_body` needs a member body anchor
   (`return ANYTHING.format("stable", ANYTHING)`).

## Design

Mirror the working var-group path (`select_min_cost_var_slot_constraints` +
`VarSlotConstraintSearch`, a weighted set-cover B&B) and generalize it to a
single **anchor-cover search** over all declaration kinds.

- **Anchor**: a renderable concrete feature at an AST path inside the target
  (a literal value, a call callee/arg literal, an object `key: literal`, a
  class member body statement, a var declarator value, ...). Each anchor has a
  cost (count of concrete retained tokens) and an **exclusion set**: the
  competitors it rules out (siblings whose corresponding position lacks it).
- **Competitors**: top-level items sharing the target's mandatory skeleton
  (same `TopLevelKind`, same function arity, ...). The `SelectorCandidateIndex`
  (PR 2251) posting lists answer "which items have feature f" in O(1), so an
  anchor's exclusion set is `competitors \ index[f]` — this is what keeps the
  search inside the time budget without re-running the matcher per candidate.
- **Search**: minimum-cost set of anchors whose exclusion sets cover every
  competitor (weighted hitting set). Greedy upper bound + branch-and-bound,
  bounded by the existing `MAX_*` node caps. Anchors that retain zero concrete
  tokens are never generated (dropping is always cheaper).
- **Render**: group chosen anchors by path and reconstruct the holey selector,
  holing every position not on a chosen anchor's path. Then **prove** with the
  production `source_match` matcher (`prove_synthesized_selector`) — the index
  is only a prefilter; correctness comes from the real match.

The index makes the candidate→exclusion step cheap; the matcher proof runs only
on the single winning candidate (plus the exact-selector fallback), not per
enumerated variant, which is the main speedup over the old enumerate-and-prove.

## Architecture (current)

- **Holing is an AST→AST prune, not a string render.** `hole_expr` / `hole_stmts`
  / `hole_object` / `hole_class_members` clone the target's `swc` subtree and
  replace dropped positions with ordinary marker nodes (`ANYTHING` ident,
  `STMT_LIST;` expr-statement, `OBJECT_PROPS` shorthand prop, `CLASS_REST;` class
  field). The holed declaration is serialized by **swc codegen**
  (`js_ast::emit_module_source`) — the one AST→string step, which the matcher's
  parse inverts. Selector and code are the same AST type; there is no second
  serializer.
- **Anchor selection** is a tiered minimum set cover (`cover_competitors` +
  `min_set_cover` B&B). Tiers: shallow literals (≤`SHALLOW_LITERAL_DEPTH` calls
  deep) → structural key/member presence → deeper literals. Within a tier, an
  exact minimum-cardinality cover avoids greedy over-pinning. Each anchor's
  exclusion set comes from the production matcher, so discrimination is exact;
  the chosen union is proven once.
- **Expectation tests compare through swc**, not text: both produced and expected
  selectors are parsed and re-emitted by codegen (`normalize_selector`), so
  formatting is irrelevant and fixtures stay prettier-managed.

## Implementation order (by tractability / shared machinery)

1. **DONE** — Statement-list / function bodies (`sparse_function_body`,
   `call_argument_literal`, `nested_async_try`).
2. **DONE** — Object literals (`object_property_literals`) via the var path
   (`minimize_var_selector`): `const X = <holed init>`, multi-key retention.
3. **DONE** — Class bodies (`class_body`) via `minimize_class_selector`:
   member-body descent, `CLASS_REST` for dropped member runs.
   The shallow-literal/structural tier split keeps the existing
   `selector_codemod_cli_test` anchor-quality guarantees (stable key over volatile
   nested-call value; global-minimum cover over greedy prefix).
4. TODO — Binding-group partitioning (`binding_group_partition`): when to group
   vs split targets.
5. TODO — Retire the legacy `render_*_selector_variants` zoo once every category
   routes through the AST-prune path (function/var/class are migrated; the legacy
   path still serves multi-target var groups and acts as a fallback).
6. TODO — Extend the proptest generator beyond functions to var/object/class.

Each step is validated against the expectation suite (compared through swc). If
the minimizer finds an equivalently-minimal-or-better shape than a fixture, the
fixture is updated to the produced `f(input)=output` (per the suite's own
preamble) rather than forcing the old bytes.
