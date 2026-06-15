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
    O(members × anchors × matching-siblings); re-dogfood the real chunk to
    confirm the wall-clock win.

## What does not work / needs work

1. **Apply is non-atomic and crashes on overlapping text edits.** Applying a
   scope aborted mid-run: `invalid or overlapping text edit 586..586` on a YAML
   that has a leading `source_match` member followed by **two `variable_declarator`
   members that get merged into one `binding_group`**. The minimization itself
   is correct; the YAML rewrite (`binding_group_text_edits` / `apply_text_edits`)
   produces colliding edit ranges when group-member removal + group insertion
   coincide in a file that also carries an unrelated `source_match` member. The
   apply had already written 145 valid files before aborting, so it is both
   buggy _and_ not transactional — it should compute all edits, detect/merge
   overlaps, and write atomically (all-or-nothing per file). A minimal
   hand-built repro (two grouped declarators + a preceding `source_match`
   member) did **not** reproduce, so the trigger is offset-sensitive to the
   real file's sizes; reduce from the real file before writing a disabled case.
2. **Large declarations over-pin via the exact-selector fallback.** A class
   among many sibling classes emitted its **full ~100-line body** instead of a
   minimized `class X { CLASS_REST; <discriminating member> CLASS_REST }`.
   `minimize_class_selector` returned `None` — its cover could not discriminate
   the class against the chunk's hundreds of sibling classes (or the search was
   bounded), so synthesis fell back to the exact full-AST selector. Aspiration:
   minimize large classes/objects to a few member/body anchors even when there
   are many same-kind siblings. Recommended disabled e2e case
   (`class_among_many_siblings`): a target class among many sibling classes that
   currently emits its full body — reduce from the real example to confirm the
   trigger (likely the cover bailing against hundreds of class competitors)
   before committing the fixture.

## Conversion committed

The 145 valid conversions from the `shared/ + integrations/ + infra/` scope
(371 name pins → 324 `source_match` + 15 `binding_groups`) were applied and
committed in the spec's private repo. Remaining scopes (`app/`, `domains/`,
`features/`) await the index-prefilter (for speed) and the overlapping-edit fix
(for clean full-scope apply).

## Suggested next steps

1. Make apply transactional + overlap-safe (unblocks full-scope `--apply`).
2. ~~Index-prefilter the cover~~ (done — see "Fix (done): index-prefilter"
   above; re-dogfood the real chunk to confirm the wall-clock budget).
3. Improve large-decl minimization so big classes/objects don't fall back to
   the full AST when many same-kind siblings exist.
