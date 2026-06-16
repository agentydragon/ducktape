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

The three-layer index and the read-off path for the first two forms are built and
behavior-preserving; the bespoke cover search still backs the unmigrated forms.

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
- **Single-target object** (`try_single_object_read_off` → `hole_object_padded`):
  always leads+trails with `OBJECT_PROPS` so the discriminating `key: value`
  matches as an interior subsequence element (survives key reorder), never
  anchored to the object's right edge.

**Selector language features the read-off renderer pins through.** All list holes
exist in `source_match_holes.rs` and the matcher (`match_list_with_holes`):
`STMT_LIST`, `ARGS`, `OBJECT_PROPS`, `DECLARATORS`, `CLASS_REST`, and `CASE_REST`
(switch-case-run — closes the survey's inexpressible many-arm-`switch` shape).
Regex-over-string-literal anchors (`STR_LITERAL_MATCHING_RE`) fire for
stable-prefix/volatile-tail strings (trailing hex/digit runs).

**Strangler-fig boundary.** The cover search (`minimize_via_retention`,
`cover_competitors`, the B&B `min_set_cover`, `collect_*_anchors`) is **not
deleted**; it backs single-target var (non-object), single-target class, and the
binding-group / multi-declarator paths, plus any tail a read-off cannot yet
single out. `selector_codemod.rs` is ~3.7k lines and shrinks substantially once
the cover is removed.

**Measured perf.** Whole ~7 MB / ~4k-member spec minimizes in ~113 s today
(pre-read-off baseline). The `≤10s` ideal is validated only on the synthetic
sweep so far; the real-chunk size-sweep is W4 (backlog).

**E2E expectation suite** (`e2e/selector_minimizer_expectation_test.rs`). Active:
`sparse_function_body`, `call_argument_literal`, `object_property_literals`,
`binding_group_declarators`, `nested_async_try`, `class_body`, `switch_case_run`,
`object_keys_over_pinned` (unignored once the object read-off landed),
`binding_group_partition`. Ignored as aspirational (the named form not yet
read-off-expressible): `class_among_many_siblings`, `sibling_subclass_hierarchy`,
`sequential_assignment_block`, `deeply_nested_call_args`, `grouped_enum_objects`.

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

## Backlog (open only)

Agreed order (interleave perf-first, then severity-ordered minimality). Counts
are members in a representative real-spec sample (5,556 selectors); the
non-minimal pattern catalog drives this and is encoded as disabled E2E cases.

1. **Perf — prove-gate via index.** Whole-chunk minimize is ~110s, almost all of
   it the per-member full-matcher proof (`prove_synthesized_selector` →
   `resolve_member_binding`) — read-off made synthesis cheap but the prove-gate is
   the bottleneck, unchanged from the 113s search baseline. Fix: the index is a
   **sound superset**, so when the selector's anchor-feature posting-list
   intersection is the singleton `{target}` that _is_ a uniqueness proof — skip
   the matcher; full matcher only for non-singleton cases. Collapses per-member
   cost for the `OPT=1` majority toward the ≤10s target.
2. **Function** whole-body anchoring (~1,455 full + ~1,743 holed; biggest count) —
   anchor a deep unique literal/member, hole the rest (`sparse_function_body`).
3. **Class** body among siblings (91 full + 268 holed; worst severity, ~1,151-line
   bodies) — `CLASS_REST` + discriminating member (`class_among_many_siblings`,
   `sibling_subclass_hierarchy`).
4. **Long-literal-value anchor** (~25; extreme severity, clean fix) — prefer a
   short discriminating key over the largest node (`long_literal_value_anchor`).
5. **Object-dict family** — over-pinned keys (`object_keys_over_pinned`, done for
   single scalar), nested-value dicts (`object_nested_value_dict`), grouped
   objects (`grouped_enum_objects`: `DECLARATORS` + padded `OBJECT_PROPS`), wide
   destructure (`wide_destructure_block`).
6. **Statement runs** (`sequential_assignment_block`, `deeply_nested_call_args`)
   and **var** migration (needs binding-group/declarator-slot tuple resolution).
7. **Anti-unification grouping** from posting co-occurrence (shared declaration OR
   minimal-selector overlap threshold) → `binding_group`.
8. **Delete the cover search** once all forms route through read-off
   (`minimize_via_retention`, `cover_competitors`, `min_set_cover`,
   `collect_*_anchors`) — shrinks `selector_codemod.rs` substantially.
9. **`selector_codemod.rs` refactor** — after cover deletion (deletion reshapes
   the file; splitting by form earlier also enables parallel per-form fan-out).
10. **Language simplification** (see below).
11. **W4 — whole-spec validation:** ≤10/30 s + real-chunk size-sweep; refresh the
    dogfood note (current real-chunk baseline ~110s recorded there).

Each capability re-applies to gaffer-private as it lands (review diffs for
over-pin; gaffer validates with a _pinned_ debundle release, so spec PRs must
regen the pipeline goldens and stay pin-compatible).

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

`ANYTHING` is already parse-position-polymorphic (see `source_match_holes.rs`):
in an expression position it behaves like `EXPR`, as a bare expression statement
like `STMT`, as a declarator name like `DECLARATORS`, as an object shorthand it
absorbs `OBJECT_PROPS`, as an init-less class field like `CLASS_REST`, etc. So in
any position where **only one kind of placeholder is syntactically legal**, the
specific keyword (`OBJECT_PROPS`, `DECLARATORS`, `CLASS_REST`, `ARGS`, ...) is
redundant with `ANYTHING`. Reducing to one placeholder where unambiguous cuts
language surface and reader ambiguity.

Do this **after** minimizer emission stabilizes (after migration + cover deletion),
since it rewrites emitted selectors and touches many fixtures:

- **Emit `ANYTHING`** from the minimizer/renderer in unambiguous positions instead
  of the specific list hole; keep the specific keyword only where the position
  genuinely admits more than one placeholder kind (document which).
- The matcher already accepts `ANYTHING` in those positions, so this is an
  emission + fixture change, not a matcher change. Confirm equivalence through
  swc; the prove-gate is unchanged.
- Decide per keyword whether to deprecate/remove it once fully subsumed (affects
  existing specs — needs a sweep) or keep it as accepted-but-not-emitted sugar.
  Default: keep accepting, stop emitting; revisit removal separately.
