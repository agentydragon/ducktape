# Dogfood: synthesize-selectors on a large real spec

First run of `debundle spec synthesize-selectors --rewrite
name-binding-to-source-match --apply` against a large real reverse-engineering
spec (a ~7 MB minified chunk; ~4.5k `binding.name` minified-name members across
~1.7k module YAMLs). Goal: mechanically replace rebuild-fragile name pins with
structural `source_match` / `binding_groups`. Anonymized; the spec lives in a
separate private repo.

## What works

- **High conversion rate.** On the scoped subtrees that completed, ~84% of
  name-pin members converted to proven structural selectors
  (`shared/`: 247 members → 207 changed, 164 of them grouped, 40 skipped;
  small leaves 60–95%). Every emitted selector is matcher-proven to resolve
  uniquely to the same binding (gate 1 holds).
- **Group minimization is good.** Multi-declarator `const`s collapse to one
  `binding_group` with `DECLARATORS_*` gaps and per-slot literals kept, e.g.
  `const DECLARATORS_BEFORE = null, hasIdleCallbackSupport = ANYTHING < "u" && …,
shouldUseIdleCallbackRendering = ANYTHING, DECLARATORS_BETWEEN = null,
deferredRenderBatchSize = 10, DECLARATORS_AFTER = null;` — keeps the
  discriminating literals (`"u"`, `10`), holes the rest.
- **Per-member cost is small.** A ~250-member subtree finishes in ~22s; ~10
  member leaves in ~3s (parse-dominated).

## What is slow (perf bottleneck)

- **The whole chunk times out (>300s) for ~4.5k members.** Parsing the 7 MB
  chunk once is ~3s; the rest is per-member cover cost. Each member's cover
  renders one selector per candidate anchor and runs the **production matcher
  over the full 7 MB AST** for each — so cost ≈ members × anchors × full-AST
  match. At ~0.08s/member that is ~350s for the whole chunk.
- **Measured hot path** (a ~45s dogfood slice; `perf` is unavailable in this
  container — `perf_event_paranoid=2`, `linux-tools` not installable — so a
  gdb-based stack sampler took 117 main-thread samples, % = stacks containing
  the frame):
  - `member_binding_candidate_matches` / `find_matching_body_ranges` (the full
    top-level scan): **56%**
  - `AstWildcardMatcher` / `PreparedNeedle::matches` (per-statement match): **47%**
  - `minimize_*`: **32%**; `cover_competitors` / `min_set_cover` /
    `matched_body_indices`: **19%**
  - parsing/lexer: **1%**; codegen/emit: **0%** — parsing is _not_ the
    bottleneck during the cover phase (the chunk is parsed once up front).
  - Slow call site: `find_matching_body_ranges` (`source_match.rs:2119`, single
    `:2136` / window `:2153` paths) scans **every** `runtime_module.body`
    top-level statement, invoked from `matched_body_indices`
    (`selector_codemod.rs`) → `member_binding_candidate_matches`
    (`source_match.rs:676`), called **once per candidate anchor per tier** by
    `cover_competitors`. Net ≈ members × anchors × (full top-level scan ×
    match-cost).
- **Fix (done): index-prefilter.** `ChunkSelectorIndex` now owns one
  `SelectorCandidateIndex`, built once per chunk and shared across every member
  and binding group (so a whole-chunk or multi-YAML batch run builds it once).
  `matched_body_indices` queries `candidate_set_for_source_match` for the
  body-index set the selector's features could still match, then calls the new
  `source_match::member_binding_candidate_matches_within(...,
BodyIndexFilter::Restricted(&candidates))`. The matcher's per-item scan loops
  (`find_matching_body_indices`, `find_matching_body_ranges`,
  `find_matching_target_var_declarators`, the declarator-hole and
  single-declarator-window paths) skip body indices the filter rejects, turning
  the inner loop from O(all top-level statements) into O(plausible candidates).
  The index is a pure prefilter: the candidate set is a sound superset, and the
  full structural matcher still proves every reported match
  (`prefilter_matches_brute_force_scan` asserts the superset and identical
  results). The final uniqueness arbitration (`resolve_member_binding`) still
  scans with `BodyIndexFilter::All` — correctness gate unchanged.
  - **Synthetic micro-measurement.** On a 251-statement synthetic chunk (200
    same-shaped sibling `const`s + 50 functions + 1 expr statement; no real
    data), a discriminating member selector that resolves to one declarator
    previously ran the matcher over all 251 top-level items per anchor; the
    candidate index narrows the scan to the single var-declaration whose
    `StringLiteral`/`ObjectKey`/`VarKind` features intersect (≈1 item), so the
    per-anchor matcher cost drops by roughly the sibling count. A holed selector
    that legitimately matches all 200 siblings narrows to exactly those 200 (the
    50 functions and the expr statement are pruned). The whole-chunk run is
    expected to fall from O(members × anchors × all-statements) toward
    O(members × anchors × matching-siblings).

## Confirmed on the real chunk (post-fix)

Re-dogfooded the whole 7 MB chunk / ~4.1k members with the prefilter, the serde
apply, and the regex-literal minimizer all in place. Wall-clock (`date`-based;
binary run directly, no Bazel overhead):

| Scope           | Files | members | seconds | s/member    |
| --------------- | ----- | ------- | ------- | ----------- |
| infra           | 53    | 14      | 3       | parse-floor |
| shared          | 115   | 98      | 4       | ~0.04       |
| app             | 221   | 940     | 30      | 0.032       |
| domains         | 497   | 1082    | 28      | 0.026       |
| features        | 817   | 1976    | 60      | 0.030       |
| **whole chunk** | 1745  | 4135    | **113** | **0.027**   |

- **Whole chunk now completes in ~113s** — was a >300s timeout. Per-member cost
  fell ~3x (~0.08 → ~0.027 s/member). Any single subtree scope lands in 28–60s,
  inside the <60s hard per-use budget; a ~3s parse floor (parsing the 7 MB chunk
  once) dominates small scopes. Prefix-scoping is no longer needed for speed,
  only for incremental review.
- **Apply is transactional (serde rewrite).** Applying a scope that includes the
  prior crash signature (a leading `source_match` member plus ≥2 grouped
  declarators, e.g. a 1 + 23-declarator file) no longer raises `invalid or
overlapping text edit`; apply is all-or-nothing per file. The whole-document
  load → mutate → dump path retired the byte-offset edit machinery that produced
  colliding ranges.
- **Regex-literal anchors fire and look right.** The var minimizer emitted
  `STR_LITERAL_MATCHING_RE(...)` ~56 times on a real scope: build-volatile
  CSS-module hashed class literals (`"<Component>-module_wrapper__<hash>"` →
  `^<Component>-module_wrapper__[A-Za-z0-9_-]+$`) and a content-hashed dynamic
  import path, anchoring the stable semantic prefix and holing the hash. Stable
  regex _literals_ in source (URL/anchor-tag patterns) are kept verbatim as
  discriminating identity — correctly not treated as volatile.
- **~93% minimal on the sampled scope.** Of changed members, only proven-unique
  single functions kept a full body; the rest used `ANYTHING` / `DECLARATORS_*`
  / `STMT_LIST` / `OBJECT_PROPS` holes (and the regex anchors above).

## What does not work / needs work

1. **Large keyed lookup objects still over-pin (pattern b).** The object
   retention path keeps every key/value instead of holing the non-discriminating
   ones with `OBJECT_PROPS`. Seen on a grouped `const` of several large
   class-name lookup objects (~13 keys each): the binding group kept all ~214 key
   lines (each value held to `ANYTHING`) with **zero** `OBJECT_PROPS` holes —
   correct and proven-unique, but wasteful and rebuild-fragile. Detect by a
   lopsided diff (hundreds of added literal-key lines for one file); such scopes
   are left out of `--apply` commits until this improves. Already captured by the
   ignored `object_keys_over_pinned` expectation case (same root cause: the cover
   treats each key/value as discriminating and never holes them); the grouped
   manifestation is the same fix, not a separate one.
2. **Large class among many siblings → full body (pattern a).** A class among
   many sibling classes can emit its **full ~100-line body** instead of a
   minimized `class X { CLASS_REST; <discriminating member> CLASS_REST }` when
   `minimize_class_selector` can't discriminate against hundreds of sibling
   classes and synthesis falls back to the exact full-AST selector. Not exercised
   in the sampled scope this run (the full-body members there were proven-unique
   single functions, not the sibling-collision pattern), but still latent.
   Captured by the ignored `class_among_many_siblings` expectation case.

## Conversion committed

- First scope: 145 valid `shared/ + integrations/ + infra/` conversions
  (371 name pins → 324 `source_match` + 15 `binding_groups`).
- Post-fix re-dogfood: 14 clean `shared/` files (proven-unique, not wasteful),
  excluding the one over-pinning grouped-object file (pattern b above).

Both batches were applied and committed in the spec's private repo. With apply
now transactional and the whole chunk under the 5-minute cap, the remaining
scopes are unblocked for review-and-commit; the only gate is reading each diff
for the pattern-b over-pin before committing.

## Read-off minimizer real-chunk perf (2026-06-16)

W4 perf acceptance measurement of the **current read-off minimizer** (shape-index
`minimal_anchor_set` + `kept_spans_for_anchor_set`, the default minimization path;
no `--no-minimize`). Dry run (no `--apply`), `name-binding-to-source-match`
rewrite, binary run directly (no Bazel overhead), wall-clock via `time.perf_counter`,
peak RSS via `getrusage(RUSAGE_CHILDREN)` on a fresh child per scope. Members
counted as `name_binding_members` (the members the minimizer actually processes).
The spec has accreted more outstanding name pins since the prior baseline, so
per-scope member counts are higher than the 2026-pre-fix table above.

| Scope           | Files | members | seconds     | s/member  | peak RSS    |
| --------------- | ----- | ------- | ----------- | --------- | ----------- |
| infra           | 53    | 155     | 6.5         | 0.042     | ~253 MB     |
| integrations    | 42    | 51      | 4.1         | 0.080     | ~253 MB     |
| shared          | 115   | 247     | 8.7         | 0.035     | ~253 MB     |
| app             | 221   | 940     | 27.0        | 0.029     | ~253 MB     |
| domains         | 497   | 1082    | 27.0        | 0.025     | ~253 MB     |
| features        | 817   | 1976    | 57.6        | 0.029     | ~253 MB     |
| **whole chunk** | 1745  | 4451    | **107–112** | **0.024** | **~253 MB** |

(Whole-chunk timed twice: 106.6s and 112.5s — stable around ~110s. Peak RSS is
flat at ~253 MB across every scope: the parsed 7 MB AST + indices dominate and the
per-member work allocates little, so memory is not scope-sensitive.)

- **Index/parse floor ≈ 3.3s.** A scope whose prefix matches zero files (0 members
  processed) still pays ~3.3s — parse the chunk once, build the candidate index and
  the read-off shape index. This one-time cost dominates small scopes and is the
  floor every invocation pays.
- **Per-member cost is ~linear and ~constant** at ~0.024–0.04 s/member once the
  parse floor is amortised (small scopes look more expensive only because the fixed
  floor is divided over few members). Whole chunk = floor + members × ~0.024s.

### Budget verdict

- **Misses the ≤10s ideal and the ≤30s hard budget on the whole chunk by a wide
  margin.** Whole chunk is ~110s — ~3.7× over the 30s hard cap and ~11× over the
  10s ideal. The read-off minimizer does **not** meet the W4 whole-chunk budget on
  real data.
- **Sub-scopes do meet budget**, with one exception: `infra`, `integrations`,
  `shared`, `app`, `domains` all land ≤30s; `features` (~58s, ~2k members) is the
  only single subtree over the hard cap. So scope-at-a-time review stays inside
  budget for every subtree except the largest, but the whole-chunk acceptance
  criterion is not met.

### Where the time goes

The read-off layer makes the **selector choice** cheap (shape-index anchor set, no
full-AST scan), but it does not remove the per-member **uniqueness proof**:
`synthesize_simplest_selector_for_group` still calls `prove_synthesized_selector`
(`source_match::resolve_member_binding{,_group_match}` — the production matcher)
for every emitted selector. That proof runs the matcher over the
candidate-index-filtered AST once per member group, and is the dominant per-member
cost. So the split is: **~3.3s one-time build (parse + candidate index + shape
index)** + **~0.024 s/member prove-gate**, the prove-gate accounting for essentially
all of the ~107s of per-member time on the whole chunk.

### Comparison to prior numbers

- **vs. 113s search-based baseline:** the whole-chunk wall-clock is **unchanged**
  (~110s vs 113s). The read-off minimizer's _selector-synthesis_ phase is cheaper
  than the old search-based cover, but because the prove-gate (full matcher, once
  per member) was already the bottleneck and is untouched, total wall-clock did not
  move. The s/member is also flat (~0.024 vs ~0.027; the small delta is noise plus
  the larger member count amortising the fixed floor better). **The read-off work
  optimised the cheap half; the prove-gate is now the entire cost.**
- **vs. synthetic `OPT=1` = 100% prediction:** the synthetic micro-benchmark
  predicted the candidate-index prefilter would collapse the per-anchor scan from
  O(all top-level statements) to O(matching siblings) — i.e. the _matcher_ call gets
  cheaper per invocation. That holds (the prefilter is in place and the proof runs
  on a filtered candidate set), but it does **not** make the whole-chunk run hit a
  10s/30s budget: the residual ~0.024 s/member × ~4.5k members = ~107s still
  dominates. The synthetic prediction was about per-call matcher cost, not about
  eliminating the once-per-member proof — the whole-chunk budget needs the proof
  itself amortised or batched across members, not just narrowed.

## Suggested next steps

1. ~~Make apply transactional + overlap-safe~~ (done — serde whole-document
   rewrite; confirmed on the prior crash signature).
2. ~~Index-prefilter the cover~~ (done — confirmed ~113s whole-chunk, ~3x
   per-member).
3. Improve large-decl minimization so big classes/objects don't fall back to the
   full AST / keep all keys when many same-kind siblings exist (patterns a + b;
   tracked by the two ignored expectation cases).
4. **Whole-chunk W4 budget (≤10s/≤30s) is not met (~110s).** The remaining cost is
   the once-per-member `prove_synthesized_selector` matcher proof, not selector
   synthesis (which read-off already made cheap). To hit budget the proof must be
   amortised/batched across members rather than run per member, or the prove-gate
   restricted to a cheaper equivalence check the read-off shape index can already
   discharge for the common (single proven-unique) cases.
