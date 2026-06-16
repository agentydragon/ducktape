# Plan: read-off selector minimization

Status: active. The agreed target architecture for the debundle selector
minimizer — a single chunk-wide AST-shape index that selectors are **read off**
rather than searched. This doc is the current state plus the open backlog; it is
not a changelog. Sibling planning docs are indexed from <../TODO.md>.

## Motivation

`debundle` reverse-engineers minified JS into named-module specs. Members pin a
runtime entity either by a fragile `binding.name` (a minified name that churns
every rebuild) or by a robust, alpha-equivalent `source_match` selector (holes +
kept anchors). The minimizer's job: mechanically turn name-pins into the
**sparsest `source_match` (or `binding_group`) that still uniquely and robustly
identifies the target** among its chunk siblings — discrimination _and_
meaningful value pinning, holes interleaved at every nesting level.

## Thesis: read off, don't search

Build **one inverted feature index over AST shapes per chunk**, then read the
minimal selector off it instead of searching per member.

- **One pass, O(N):** walk every subtree once; at each node emit position-aware,
  **alpha-equivalent** shape features (minified identifiers holed; stable things
  kept): shallow literal values, object keys, member/method names, callee
  identities, bounded-depth shape skeletons.
- **Posting lists:** feature → set of items (bindings / top-level statements)
  exhibiting it — the "pseudo-trie generalized to AST trees": index subtrees by
  structural prefix (path-from-root + shape); the leaves are the items sharing
  that shape.
- **Selector = conjunction of features**, whose match set is the intersection of
  posting lists. A _minimal_ selector for a target is the smallest subset of the
  target's own features whose intersection is the singleton `{target}`.
- **Read-off:** scan the target's features (O(target size)), rank by **selective
  × stable**; if the most selective+stable feature's posting list is already
  `{target}`, done in one anchor. Most real code has such a feature, so the
  common case is a read-off, not a search.
- **Tail:** when no small stable combination is singleton, fall to a _bounded_
  intersection over the target's few features (greedy-near-optimal via
  selectivity ranking). Never an unbounded chunk-wide search.
- **Prove-gate stays:** the production matcher confirms the read-off selector
  resolves uniquely. It is the correctness gate, never the search engine.

Net cost: `O(N + Σ target sizes) ≈ O(M+N)` — linear in chunk + spec size.

Two things this makes principled, not bolted-on:

- **Grouping overlapping captures.** Targets that would get near-identical
  selectors co-occur in the same posting lists — a lookup, not a heuristic.
  Group when targets **share an enclosing declaration OR their minimal selectors
  overlap beyond a threshold**, emitting a `binding_group` (with `DECLARATORS`
  holes) instead of N overlapping standalone selectors.
- **Forward-compatibility.** Feature ranking is two-key (**selective × stable**):
  prefer semantic literals, exported names, structural shape; deprioritize
  minified names and volatile hashes (hole them, or regex-anchor a stable prefix
  per the `STR_LITERAL_MATCHING_RE` path). Minimal + stable = survives rebuilds.

## Validated architecture (three layers)

The literature spike (<readoff_algorithm_research.md> + five strand reports in
<readoff_research/>) is complete and confirms the **three-layer architecture**:

1. **Canonicalization + shape index (O(N)).** Hash-consed Merkle DAG
   (Downey-Sethi-Tarjan) with alpha-leaf canonicalization; multi-granularity
   shape features; inverted posting lists; selectivity (free byproduct) +
   stability scores. Supersets `SelectorCandidateIndex`.
2. **Read-off minimization (greedy set-cover, two-key `selective × stable`).**
   `OPT=1` read-off in the Zipfian common case; bounded greedy tail otherwise.
   Minimization reduces _exactly_ to Minimum Set Cover (NP-hard, W[2]-complete);
   greedy is provably near-optimal, so "mostly minimal" is the correct target,
   not a compromise. The production matcher is the prove-gate, never the engine.
3. **Grouping (n-ary anti-unification / LGG).** Linear; co-occurrence in posting
   lists detects overlap.

**Locked decisions:**

- **List-hole encoding = cons-spine binarization + bounded-depth shape
  skeletons, behind a swappable feature-extraction interface.** No arity
  assumption is baked into the index or greedy core; variadic handling lives
  behind the `ShapeFeatureExtractor` trait (default `ConsSpineExtractor`) and the
  matcher-verify boundary, so it can later be swapped for hedge automata
  (TATA ch. 8) _iff_ deep list-body matching proves load-bearing.
- **Migration is strangler-fig.** The index + read-off path runs alongside the
  existing cover search; forms migrate one at a time keeping all tests green; the
  old search is deleted only once everything routes through read-off.
- **Grouping trigger:** shared enclosing declaration OR minimal-selector overlap
  beyond a threshold.
- **Old pins:** replace fragile gaffer-private pins in the same wave as each
  scope re-minimizes cleanly.

## Current state (on `devel`)

The three-layer index and the read-off path back every single-target form
(function, class, object, var); the branch-and-bound cover search they replaced is
deleted. The only remaining bespoke cover is the multi-target var binding-group
keep-shallow path.

**Layer 1 — shape index.** `shape_index.rs` builds the hash-consed Merkle DAG with
alpha-leaf canonicalization, multi-granularity shape features, inverted posting
lists, and `selective × stable` scoring; `minimal_anchor_set(item) -> AnchorSet`
is the read-off API. Built on `selector_candidate_index.rs` (the prefilter
`SelectorCandidateIndex`), which it supersets, not forks. Soundness is gated by
`shape_index_soundness_test.rs` (matcher-backed: the indexed candidate set is
always a sound match superset) and a synthetic size-sweep + OPT-distribution
benchmark in `shape_index_bench.rs`. Measured (synthetic, N=200/1000/4000): build
is linear (~0.034-0.051 ms/item; ~1.27 distinct shapes/item) and among resolvable
items the **OPT=1 share is 100%**, validating the Zipfian `OPT=1`-majority
assumption that underwrites the near-linear `≤10s`-ideal target. Greedy set-cover
seeds `covered` from the smallest relevant posting list, so unresolvable items pay
O(smallest posting) per step, not O(N).

**Feature taxonomy.** `SelectorFeature` indexes string, **number, and boolean**
literals (number/bool canonicalized and scored `Semantic`; never wildcarded — the
matcher discriminates by `eq_ignore_span`, so the candidate set stays a sound
match superset), object keys, member/method/callee names, and bounded-depth
skeletons. High-multiplicity skeletons (interned >4×) demote to `Structural` so
value anchors win stability ties.

**Layer 2 — read-off renderer + migrated forms.** `readoff_render.rs`
(`kept_spans_for_anchor_set`) maps a read-off `AnchorSet` to the kept-span set the
existing `selector_codemod` prune + swc-codegen machinery consumes — no second
serializer; skeleton/arity anchors pin via the holed scaffold (no kept span).
Migrated to read-off as their primary path:

- **Single-target function** (`minimize_function_selector` → `render_via_read_off`):
  reads off `minimal_anchor_set`, prefers the empty-kept structural scaffold when
  it already discriminates, renders through the shared `render_with`.
- **Single-target object** (`try_object_read_off` → `hole_object_padded`):
  surrounds every kept prop with `OBJECT_PROPS` (leads, trails, **and**
  interleaves) so a discriminating `key: value` — or a minimal _key set_ — matches
  as independent interior subsequence elements (survives key reorder and gaps),
  never anchored to the object's right edge or assuming two kept keys stay
  adjacent. Handles the object whether it stands alone or sits in a
  multi-declarator group (one target slot, `DECLARATORS_*` holes for the rest):
  reads its anchors off the shape index, then falls back to a slot-aware key-set
  cover (`cover_object_slot`) that singles out the target declarator's own keys —
  the per-slot view the chunk-wide read-off cannot see.
- **Single-target class** (`minimize_class_selector` → `render_via_read_off`):
  holes `extends` to `ANYTHING` and keeps only the member runs carrying a chosen
  anchor between `CLASS_REST` holes; the value-over-name ranking key prefers a
  member's value literal/key over a bare member name.
- **Single-target var (non-object)** (`try_var_read_off`): holes the initializer
  with `hole_expr` around the read-off anchors, restricting kept spans to the
  target declarator; drills into nested call/object/array trees down to the
  discriminating leaf. No empty-kept fast path — a var's holed scaffold
  (`const X = ANYTHING`) is degenerate, so an empty anchor set defers to the
  keep-shallow group path. The `STR_LITERAL_MATCHING_RE` upgrade is shared with
  the group path via `accepted_regex_anchors`.

**Selector language features the read-off renderer pins through.** All list holes
exist in `source_match_holes.rs` and the matcher (`match_list_with_holes`):
`STMT_LIST`, `ARGS`, `OBJECT_PROPS`, `DECLARATORS`, `CLASS_REST`, and `CASE_REST`
(switch-case-run — closes the survey's inexpressible many-arm-`switch` shape).
Regex-over-string-literal anchors (`STR_LITERAL_MATCHING_RE`) fire for
stable-prefix/volatile-tail strings (trailing hex/digit runs).

**Strangler-fig boundary.** The matcher-driven branch-and-bound cover search
(`minimize_via_retention`, `cover_competitors`, `min_set_cover`, and the
function/class anchor collectors) is **deleted**: single-target function, class,
object, and var all route through the read-off, which subsumes it once W3 added
number/bool features (its last reason to exist). A target the read-off cannot
single out is reported as debt, never full-AST-pinned. The one cover that
remains is the **multi-target var binding-group keep-shallow path**
(`minimize_var_group_selector` + `collect_expr_anchors` +
`AnchorCandidates::{shallow_literals,deep_cover_tiers}`), which does the per-slot
tuple resolution the chunk-wide read-off cannot express.

**Measured perf.** Whole ~7 MB / ~4.5k-member chunk minimizes in **~13 s** with
the prove-gate-via-index fast-path (#2280), down from ~110 s — **meets the ≤30 s
hard budget**, narrowly over the ≤10 s ideal (per-member ~0.0027 s, ~9× cheaper).
Full scope table in <../debug/selector_minimizer_dogfood.md>. #2291 then closed
the index-build cost (`roaring::RoaringBitmap` posting lists + `FxHashMap` inverted
indices): ≈1.8× faster build / 4.5× faster read-off on the synthetic sweep, and on
the current (smaller, partly dogfood-applied) spec the whole chunk minimizes in
**~7 s**, comfortably under the ≤10 s ideal.

**E2E expectation suite** (`e2e/selector_minimizer_expectation_test.rs`). Active:
`sparse_function_body`, `call_argument_literal`, `object_property_literals`,
`binding_group_declarators`, `nested_async_try`, `class_body`, `switch_case_run`,
`object_keys_over_pinned`, `long_literal_value_anchor`, `binding_group_partition`,
`class_among_many_siblings`, `sibling_subclass_hierarchy`, `adjacent_accessor_group`,
`interior_object_arg_holing` (unignored once the `Expr::Array` interior holing
landed, #2289), and (unignored once the object key-set cover landed)
`grouped_enum_objects`, `object_key_set_group`, `object_key_set_subset`. Ignored as
aspirational (the named form not yet read-off-expressible):
`sequential_assignment_block`, `deeply_nested_call_args`, `object_nested_value_dict`,
`wide_destructure_block`, `single_target_class_whole_body`,
`component_wide_destructure_whole_body`.

## Acceptance criteria

1. **Perf** — whole ~7 MB / ~4k-member spec minimizes in **≤10 s ideal, ≤30 s
   hard** (from 113 s); linearity shown by a real-chunk size-sweep.
2. **Minimality** — emitted selectors are minimal-or-near (metric: retained
   AST-node count vs the read-off lower bound). **Hard rule: never dump an
   untrimmed AST.** `--full-ast-fallback` stays off by default; a run _reports_
   any member it could not minimize instead of dumping the full AST.
3. **No overlapping captures** — zero large-overlap standalone selector pairs on
   the dogfood spec; such cases become `binding_group`s.
4. **Coverage** — var (single + group), function, class, object, including the
   large-object / large-class cases that currently over-pin; the ignored E2E
   cases get unignored as capabilities land.
5. **Forward-compat** — stable-anchor preference, validated by a
   rebuild-perturbation test (perturb volatile fragments → selector still
   resolves).
6. **Code health** — one unified minimization path; the cover search is deleted
   once everything routes through read-off. STYLE-clean.
7. **gaffer-private** — apply across the whole spec, replacing fragile
   `binding.name` pins and exact-minified-name pins; quantify the fragile-pin
   reduction.

**Non-goals / accepted imperfections:** perfect cardinality-minimality on
adversarial tail cases (near-minimal is fine); preserving YAML comments (out via
serde); embedded/non-trailing volatility in regex anchors (future).

## Remaining minified-pin debt (measured, gaffer `main` after #360)

`spec selector-debt` on the converted spec: **5,108 robust `source_match`** vs
**2,226 still-fragile name-only** pins (~70% converted). Re-running the minimizer
(`synthesize-selectors`, dry) over the 2,226 splits the debt by _why_ it remains
— the prioritization input for the waves below:

- **~2,024 (~91%) — "no sparse selector" (minimizer skips).** No compact
  discriminating anchor set resolves uniquely, so with `--full-ast-fallback` off
  the member is left as a name pin. These are **whole-body-only** members
  (uniqueness needs ~the entire body). Concentrated in `features` (955),
  `domains` (462), `app` (425). Recovering them is the hard, high-count tail:
  needs **interior holing of whole bodies** (backlog item 1 — keep the body but
  hole non-identifying subtrees so a fuller pin is at least less fragile / can be
  emitted) and/or deeper anchoring. Biggest number, hardest; do not expect a
  clean compact selector for most.
- **~200 (~9%) — convertible but >30 lines (filtered out of #360).** The minimizer
  _does_ synthesize a selector; it's just over the compact threshold. Shape mix:
  **~55% var/object, ~42% function, ~3% class**; median ~35 lines, tail to ~700.
  These are the **achievable near-term wins**: the over-pin-reduction waves shrink
  them under the threshold so they ship as compact selectors —
  - var/object (the plurality) → the remaining object-dict forms (backlog item 2);
  - function → **interior holing** within kept bodies (backlog item 1);
  - a re-apply after each wave reconverts whatever now fits ≤30 lines.
- 1 — `async` parse edge case (single; ignore).

**Takeaway:** the ~200 bulky-convertible are the cheap recovery (over-pin waves,
mostly object-shaped); the ~2,024 no-sparse are the structural long tail (whole-
body interior holing + anchoring). Re-measure this split after each wave lands.

## Backlog (open only)

Severity-ordered minimality work. Counts are members in a representative
real-spec sample (5,556 selectors); the non-minimal pattern catalog drives this
and is encoded as disabled E2E cases. Completed items are removed, not annotated;
the landed architecture is in "Current state" above.

1. **Candidate-index anchor deepening for no-sparse-anchor bodies (the dominant
   lever; GitHub #2289).** Two render-side pieces have landed: `hole_expr` recurses
   into function/arrow-valued subexpressions (#2301), and the **robustness-anchor
   policy** keeps a holed-down value anchor instead of the degenerate bare scaffold
   when one is available (#2303). The remaining blocker is that the candidate index
   does **not collect anchors inside class method bodies or function-expression
   bodies**, so for the whole-body cases the read-off has no value anchor to prefer
   and still falls back to the scaffold (disabled fixtures
   `single_target_class_whole_body`, `component_wide_destructure_whole_body`; and
   real-spec deep anchors like `nr_name === "HAS IMAGE"`, `n.BundleInstaller`
   wrongly bucketed "no sparse selector"). Deepen the index to collect those deep
   anchors, then add interior set-cover at subtree granularity. Reclaims a
   meaningful share of the ~2,024 "no sparse selector" tail, not just the ~200
   bulky-convertible.
2. **Object-dict family — remaining forms.** Nested-value dicts
   (`object_nested_value_dict`: hole all but one anchored nested property) and wide
   destructure (`wide_destructure_block`: `OBJECT_PROPS` around the one
   discriminating destructured property). The `ee({ coreMessage: …, type: … })`
   schema-object-call form is a call kept whole — folds into item 1.
3. **Multi-target var binding-group read-off.** Groups still use the keep-shallow
   path (`minimize_var_group_selector`) because per-slot declarator-tuple
   resolution is something the chunk-wide read-off cannot express. Designing a
   per-slot anchor union proven through the binding-group matcher would let groups
   read off too — the last consumer of the keep-shallow cover and the gate for
   items 6/7 below.
4. **Statement runs** (`sequential_assignment_block`): `STMT_LIST` holes on both
   sides of the one assignment whose RHS carries the discriminating literal.
5. **General co-occurrence grouping → `binding_group`.** The shared-declaration
   trigger (multi-declarator var) and adjacent same-shape function runs have
   landed; the general co-occurrence trigger for non-function runs (statement runs,
   sibling object/class declarations that are not a single var statement) is open.
6. **Retire the keep-shallow group cover** once item 3 lands — removes
   `minimize_var_group_selector`'s escalation path, `collect_expr_anchors`, and
   `AnchorCandidates::{shallow_literals,deep_cover_tiers}`.
7. **`selector_codemod.rs` by-form split** — the file is ~4.2k lines; splitting by
   form enables parallel per-form fan-out (do after item 6 reshapes the var path).
8. **Language simplification** (see below) — emit anonymous `OBJECT_PROPS` /
   `DECLARATORS` / `CLASS_REST` / `EXPR` / `STMT` as `ANYTHING`. Deferred until
   emission stabilizes (after the cover/keep-shallow paths fully retire), since it
   rewrites emitted selectors and touches many fixtures.
9. **`deeply_nested_call_args` — callee/arg holing.** The var read-off drills to
   the leaf but keeps bare-function callees pinned (`hole_callee` keeps a bare
   function reference) and holes dropped args to arity-exact `ANYTHING` rather than
   a variadic `ARGS` run-hole. Closing both is a cross-cutting `hole_callee` /
   `hole_args` policy change affecting all read-off paths; weigh against existing
   fixtures.
10. **Dogfood-apply on gaffer-private (ongoing).** Run `synthesize-selectors
--apply` on the real spec after each wave, review for over-pin, and PR the
    beneficial minimized selectors. Operational PR rule: **revert any converted
    selector whose `match` block is >40 lines AND has ≤2 holes** back to a name pin.
    Keep pin-compatible (gaffer validates with a _pinned_ debundle release) and
    regen the pipeline goldens. Each capability above re-applies here as it lands.

## Orchestration & process notes

Same-file overlap across waves is a merge cost, not a serialization constraint:
run each in its own `git worktree` off `devel` and integrate via a **train** —
land the deepest first (the function/sparse-anchor wave reshapes read-off depth +
renderer holing the others build on), rebase the rest onto it, resolve the
localized per-form conflicts, verify gate-1 and equivalent-or-better through swc,
unignore each E2E case as its fix lands. Off-path big-file refactors
(`purity`/`graph`/`facts`/`peel`) parallelize freely (disjoint files).

Hard-won rules for this environment:

- **Build/test with `bazelisk` + the session bazelrc + system Java + RBE, never
  `bbr`** (its git-state mirroring is broken here). One worktree + one unique
  `--output_base` per agent.
- **Agents must not spawn their own background sub-agents.** Nested background
  fan-out has stranded work repeatedly: the parent idles at the integration step
  (no BUILD wiring / no test / no push). An agent does its own work and completes
  build+push itself.
- **Background agents do not survive a session suspend/resume** — they die
  silently (no worktree, branch, or process) before pushing. Verify liveness by
  checking for worktrees/branches/processes, not "no notification = alive".
  Durable progress needs foreground work that commits+pushes incrementally, or a
  session kept warm long enough for a background run (~45 min) to finish.
- **gaffer ↔ ducktape:** gaffer validates specs by regenerating pipeline goldens
  with a pinned debundle _release_; a spec change must regen+commit those goldens
  and use only selectors the pinned release accepts (else bump the pin). Never let
  real Tana data into ducktape — anonymize reductions for E2E fixtures.

Lower priority / opportunistic:

- Regex-literal anchors: embedded / non-trailing volatility, GUID/base64 shapes
  (currently trailing hex/digit only).
- Skeleton-feature stability: refine further by multiplicity / depth.

## Language simplification: prefer `ANYTHING` where the context is unambiguous

`ANYTHING` is parse-position-polymorphic (see `source_match_holes.rs`), but its
behavior is **not** uniformly "the list hole legal here". Verified against the
matcher (`source_match/matcher.rs`, `source_match/holes.rs`,
`source_match/wildcard_idents.rs`), the decisive fact is:

> **`ANYTHING` is a run-absorbing list hole only in the positions where the
> list-hole detector predicate carries an explicit `ANYTHING` fallback.** In
> every other position a bare `ANYTHING` collapses to a _single-node_ hole
> (`EXPR` / `STMT`) — or is not expressible at all.

The detector predicates and their `ANYTHING` handling:

| Position            | Detector (in `holes.rs`)         | `ANYTHING` fallback?      | Slice router (`matcher.rs`)                 |
| ------------------- | -------------------------------- | ------------------------- | ------------------------------------------- |
| object property     | `object_property_list_hole_name` | **yes** (`.or_else`)      | `match_prop_or_spread_slice`                |
| variable declarator | `declarator_list_hole_name`      | **yes** (`.or_else`)      | `match_var_declarator_slice_with_alignment` |
| class member        | `is_class_rest_hole`             | **yes** (`\|\| ANYTHING`) | `match_class_member_slice`                  |
| call/`new` argument | `argument_list_hole_name`        | **no**                    | `match_expr_or_spread_slice`                |
| block statement     | `statement_list_hole_name`       | **no**                    | `match_stmt_slice`                          |
| `switch` case       | `is_case_rest_hole`              | **no**                    | `match_switch_case_slice`                   |

Why the single-node collapse happens for `ARGS`/`STMT_LIST`: the collector
(`WildcardIdentCollector`) visits a bare `ANYTHING` argument via
`visit_expr_or_spread`, finds no `argument_list_hole_name` match, recurses into
the child expression, and registers it as an **expression** hole; likewise a bare
`ANYTHING` expression-statement is registered as a **statement** hole (the
`hole_name == ANYTHING` branch in `visit_stmt`, which is reached only _after_
the `STMT_LIST` check fails). A single-node hole then routes through the
length-exact `match_slice` path (arity must equal), never the
ordered-subsequence `match_list_with_holes` path. `case CASE_REST:` has no
`ANYTHING` spelling at all (`is_case_rest_hole` matches only the literal
`CASE_REST` test ident; a bare `ANYTHING` statement is not a `case` clause).

### Per-keyword redundancy table

| Keyword        | Redundant with bare `ANYTHING`? | Unambiguous position               | Equivalence / inequivalence (evidence)                                                                                                                                                                                                              |
| -------------- | ------------------------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EXPR`         | **yes** (bare/anon form)        | any expression position            | `ANYTHING` ≡ `EXPR`; both anonymous single-expr (`bind_expr`). Tests: `member_source_match_anything_matches_expression_subtrees`, `single_node_hole_keeps_later_identifiers_aligned`. Named `EXPR_x` (cross-occurrence binding) is NOT replaceable. |
| `STMT`         | **yes** (bare/anon form)        | bare expression-statement position | `ANYTHING;` ≡ `STMT;`; both anonymous single-stmt (`bind_stmt`). Test: `stmt_and_anything_agree_on_a_single_statement_block`. Named `STMT_x` is NOT replaceable.                                                                                    |
| `OBJECT_PROPS` | **yes**                         | object-literal shorthand property  | run-absorber; `object_property_list_hole_name` has `ANYTHING` fallback. Test: `object_props_and_anything_are_interchangeable_run_absorbers` (+ existing `member_source_match_anything_object_property_hole_skips_arbitrary_key_values`).            |
| `DECLARATORS`  | **yes**                         | variable declarator name           | run-absorber; `declarator_list_hole_name` has `ANYTHING` fallback. Test: `declarators_and_anything_are_interchangeable_run_absorbers` (+ existing `member_source_match_anything_declarator_*`).                                                     |
| `CLASS_REST`   | **yes**                         | no-init class field                | run-absorber; `is_class_rest_hole` matches `CLASS_REST` or `ANYTHING`. Test: `class_rest_and_anything_are_interchangeable_run_absorbers` (+ existing `member_source_match_anything_class_member_*`).                                                |
| `ARGS`         | **NO**                          | call/`new` argument                | `ANYTHING` arg = single `EXPR` (arity-exact), NOT a run-absorber. Diverges on arity ≠ 1. Tests: `args_run_absorber_is_not_redundant_with_anything_single_arg` (diverge), `args_and_anything_agree_on_a_single_argument_call` (agree at arity 1).    |
| `STMT_LIST`    | **NO**                          | block statement                    | `ANYTHING;` stmt = single `STMT` (arity-exact), NOT a run-absorber. Diverges on length ≠ 1. Tests: `stmt_list_run_absorber_is_not_redundant_with_anything_single_stmt` (diverge), `stmt_and_anything_agree_on_a_single_statement_block` (agree).    |
| `CASE_REST`    | **NO**                          | empty `switch` case clause         | No `ANYTHING` spelling exists for a case-list hole; the marker is a `case` clause, not an identifier expression. Test: `case_rest_is_not_expressible_via_anything`.                                                                                 |

### Emission rule (the deferred change)

Mechanical and unambiguous given the table — for the minimizer/renderer
(`selector_codemod.rs` / `readoff_render.rs`), in position **P**:

- **OBJECT_PROPS / DECLARATORS / CLASS_REST**: an _anonymous_ (unsuffixed) hole
  in P may be emitted as `ANYTHING` instead of the keyword. A **named** list
  hole (`OBJECT_PROPS_MID`, `DECLARATORS_AFTER`) carries a readability label and
  is not equality-binding, so substituting `ANYTHING` is still semantically
  safe but loses the label — keep the keyword when a suffix is present, or
  accept the label loss as a deliberate readability tradeoff (decide at
  emission time; the matcher accepts both).
- **EXPR / STMT**: an _anonymous_ hole may be emitted as `ANYTHING`. A **named**
  hole (`EXPR_LEFT`, `STMT_SETUP`) binds for cross-occurrence equality and is
  **not** replaceable — `ANYTHING` is always anonymous (see `bind_expr` /
  `bind_stmt`).
- **ARGS / STMT_LIST / CASE_REST**: **never** emit `ANYTHING` in these
  positions. `ARGS`/`STMT_LIST` would silently change a run-absorber into an
  arity-exact single-node hole (a correctness regression), and `CASE_REST` has
  no `ANYTHING` form. These keywords stay load-bearing.

Do this **after** minimizer emission stabilizes (after migration + cover
deletion), since it rewrites emitted selectors and touches many fixtures:

- The matcher already accepts `ANYTHING` in the redundant positions, so this is
  an emission + fixture change, not a matcher change. Confirm equivalence
  through swc; the prove-gate is unchanged.
- Decide per keyword whether to deprecate/remove it once fully subsumed (affects
  existing specs — needs a sweep) or keep it as accepted-but-not-emitted sugar.
  `ARGS`, `STMT_LIST`, and `CASE_REST` are NOT subsumable and must be kept.
  Default for the rest: keep accepting, stop emitting; revisit removal
  separately.

### Matcher gap note

No matcher change is needed or proposed. The asymmetry (list-hole fallback for
`OBJECT_PROPS`/`DECLARATORS`/`CLASS_REST` but not `ARGS`/`STMT_LIST`/`CASE_REST`)
is the _reason_ `ARGS`/`STMT_LIST`/`CASE_REST` are not droppable, not a bug to
fix: making `ANYTHING` a run-absorber in argument/statement position would make
it impossible to write a single-node `EXPR`/`STMT` hole anonymously there, since
both spell as a lone `ANYTHING`. The two keyword families intentionally cover
different cardinalities.
