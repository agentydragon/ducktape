# F5 — `selector_codemod` minimizer migration off `AstWildcardMatcher`

Scope: the `selector_codemod` minimizer (the F5 step-1/step-4 user of the
matcher, alongside the candidates surface and `body_debt` diagnostics that two
sibling agents own). This note answers: what does the minimizer use from the
matcher, can `ChunkResolver` (the fact resolver) serve it, and the exact
migration.

## VERDICT: NEEDS-CANDIDATES

The fact resolver can serve the minimizer **once it grows a candidate-list
method** — `ChunkResolver::member_candidates(...) -> Result<Vec<MemberBindingMatch>>`
(or `Vec<ResolvedMemberBinding>` plus the body index per match). This is exactly
the surface the runbook's F5 step 1 already plans to add for the candidates
reroute (`plans/selector_resolver_endpoint.md` lines 200–210). The minimizer is
**blocked on that method existing**; it does not need near-miss/`body_debt`
introspection, and its group-resolution need is already served. The blocking
interface is specified precisely below so the migration can land the moment the
sibling candidates work exposes it.

It is **not GO** today only because `ChunkResolver` currently exposes just the
_categorical_ `resolve_member` (unique→`Ok(binding)`, 0/many→`Err`), which
collapses the match count and drops the per-candidate `(body_idx, binding_name)`
the minimizer's greedy search reads. It is **not NO-GO**: nothing the minimizer
asks of the matcher is AST-walk-shaped introspection — it only needs the match
_set_, which the fact resolver already computes internally (it just wraps it in
the categoricity arbitration before returning).

## What the minimizer actually uses from the matcher

Every matcher touch in the codemod crate funnels through **three wrapper
functions defined in `selector_codemod.rs`**; the `minimize/` submodules never
call `source_match::*` or `AstWildcardMatcher` directly.

| Wrapper (in `selector_codemod.rs`)                        | Underlying `source_match::` call                                                                                                               | What it returns / is read for                                                                                                                                                                           |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `matched_binding_candidates` (`selector_codemod.rs:1596`) | `member_binding_candidate_matches_within(.., BodyIndexFilter::Restricted(candidate_set))` (`:1615`)                                            | **Full candidate list** `Vec<MemberBindingMatch>` (`{body_idx, binding}`). Read for `matches.len()` (greedy score), `matches.iter().any(target is among them)`, and `[single]` body_idx/binding checks. |
| `matched_body_indices` (`selector_codemod.rs:1626`)       | wraps `matched_binding_candidates`                                                                                                             | the **set** of `body_idx` the selector resolves to (`BTreeSet<usize>`).                                                                                                                                 |
| `prove_synthesized_selector` (`selector_codemod.rs:1360`) | single target: `member_binding_candidate_matches_within(.., Restricted)` (`:1444`); ≥2 targets: `resolve_member_binding_group_match` (`:1383`) | proves the synthesized selector resolves **uniquely to the intended `decl.body_idx` and the intended runtime binding**; returns `candidate_count: usize`.                                               |

`member_binding_candidate_matches_within` → `find_member_binding_matches`
(`source_match/target_matching.rs:71`) **is** the `AstWildcardMatcher` entry. So
the minimizer's entire coupling to the hand-rolled matcher is: the candidate-list
function (twice) + the categorical group resolver (once).

### The semantic need (precise predicate)

A minimizer shrinks a candidate selector while preserving "resolves uniquely to
the same target." The greedy anchor search (`minimize/object.rs:72-99`,
`minimize/group.rs:207-238`, `minimize/mod.rs:180`) needs, per trial selector
`S` with intended target `(decl.body_idx, runtime_binding)`:

1. **The match count** `|matches(S)|` — used as a _ranking score_
   (`score = (target_unresolved, matches.len())`) to pick the anchor that "rules
   out the most competitors." Required **even while >1** (mid-search S is not yet
   unique), so a categorical unique/ambiguous/no-match verdict is insufficient.
2. **Whether the intended target is among the matches** —
   `matches.iter().any(|m| m.body_idx == decl.body_idx && m.binding.binding_name == runtime)`
   (the `target_unresolved` flag).
3. **The unique resolve check** — `[single]` and `single.body_idx == decl.body_idx
&& single.binding.binding_name == runtime` (the loop's termination /
   `slot_resolves`).

(1)+(2) need the **candidate list with per-match `(body_idx, binding_name)`** —
this is the gap. (3) alone the categorical `resolve_member` could answer, but the
search can't run on (3) alone.

`prove_synthesized_selector`'s **group** path (≥2 targets) needs exactly
`ResolvedMemberBindingGroup { body_idx, bindings }` — **already served** by
`ChunkResolver::resolve_member_group` (carries `body_idx` and per-target
bindings; categoricity matches production by construction, proven by the corpus
differential). Its **single-target** path needs the candidate count + the single
match's `(body_idx, binding)` — same candidate-list need as above.

### Does `ChunkResolver` answer it directly? No (today).

`ChunkResolver::resolve_member` (`source_match/datalog_resolver.rs:860`) returns
`Result<ResolvedMemberBinding>` — unique binding or error. It internally computes
the full match vec in every sub-path (`resolve_member_multi`,
`resolve_var_declarator_member`, `resolve_declarator_hole_member`, the
single-statement arm) and _then_ applies the `[single]`/`[]`/`multiple`
arbitration, discarding the count and the per-candidate body indices. So the
information the minimizer needs is computed and thrown away — exactly why the
runbook proposes refactoring those sub-paths to return their `matches: Vec<…>`
and adding a `member_candidates` wrapper that returns the vec.

## The interface the minimizer needs (blocking dependency — sibling candidates work)

Add to `ChunkResolver` (do **not** design it here; this is the contract the
minimizer consumes):

```rust
/// Every member-binding match the selector resolves to in this chunk, without
/// the exactly-one arbitration `resolve_member` applies (the fact analogue of
/// `member_binding_candidate_matches`). Used by callers that need the count /
/// the set / "is the intended target among them," not just the unique winner.
pub fn member_candidates(
    &self,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>>;
```

Requirements the minimizer relies on:

- Returns `MemberBindingMatch { body_idx, binding }` per match (the minimizer
  reads both fields). Returning `Vec<ResolvedMemberBinding>` is **not enough** —
  `m.body_idx` is read at every call site.
- Match **count and membership** must equal the matcher's
  `member_binding_candidate_matches` over the same chunk (the categoricity the
  greedy score depends on). This is why the runbook gates step 1 on a new
  **candidate-list differential** (fact list == matcher list over the real
  corpus), not just the existing categorical corpus differential — the corpus
  differential only proves count+winner agree, not full-list equivalence. The
  minimizer migration must not land before that differential is green.
- No external `BodyIndexFilter` needed: `ChunkResolver` already prefilters
  internally via its token index (`candidate_bodies` / `candidate_declarators`,
  `datalog_resolver.rs:145-195`), which subsumes the `SelectorCandidateIndex`
  prefilter the matcher path bolts on. The fact resolver is the per-chunk
  build-once model the minimizer's perf notes want anyway.

If the sibling agents instead expose `member_candidates` returning
`Vec<ResolvedMemberBinding>` (the runbook's literal step-1 signature), the
minimizer additionally needs the body index per match; ask them to return
`MemberBindingMatch` (or add `body_idx`) — flag this as the one signature nuance.

## Migration plan for `selector_codemod.rs`

Precondition: `ChunkResolver::member_candidates` exists and its candidate-list
differential is green (sibling candidates work, F5 step 1).

1. **Build one `ChunkResolver` per chunk in `ChunkSelectorIndex`.** Add a field
   (e.g. `resolver: ChunkResolver<'static>` borrowing `parsed.module`, or build
   it lazily) alongside `candidate_index` / `shape_index` in
   `ChunkSelectorIndex::new` (`selector_codemod.rs:978-1007`). It must borrow the
   same `parsed.module` every wrapper already matches against. (Lifetime note:
   `ChunkSelectorIndex` owns `parsed`; the resolver borrows `parsed.module`, so
   either store the resolver behind the same owner or build it on demand inside
   the wrappers from `&index.parsed.module` — building per call is acceptable
   since the fact EDB build is the cheap part vs. the matcher; prefer build-once
   if the borrow checker allows.)

2. **`matched_binding_candidates` (`:1596-1621`)** → replace the
   `candidate_index.candidate_set_for_source_match(...)` +
   `member_binding_candidate_matches_within(.., Restricted)` body with a single
   `index.resolver.member_candidates("<selector minimization>", &selector)`.
   Drop the `SelectorCandidateIndex` lookup here (the resolver prefilters
   internally). Signature/return type unchanged (`Vec<MemberBindingMatch>`), so
   `matched_body_indices` and the `minimize/{object,group}.rs` callers are
   untouched.

3. **`prove_synthesized_selector` (`:1360-1470`)**:
   - **≥2 targets branch (`:1383`)** → replace
     `source_match::resolve_member_binding_group_match(&index.parsed.module, ..)`
     with `index.resolver.resolve_member_group("<selector synthesis>", &selector,
&exports_by_target)`. Same `ResolvedMemberBindingGroup` shape; the existing
     `matched.body_idx` / `matched.bindings.get(...)` checks are unchanged.
   - **single-target branch (`:1439-1453`)** → replace the candidate-set +
     `member_binding_candidate_matches_within(.., Restricted)` with
     `index.resolver.member_candidates("<selector synthesis>", &selector)`; keep
     the `[candidate]` uniqueness check, the `candidate.body_idx == decl.body_idx`
     check, and the `binding_name == runtime_binding` check verbatim. `candidate_count`
     stays `matches.len()`.

4. **`match_selector.rs` (sibling file in the same crate, `:120-135`, `:173`)**
   uses `member_binding_candidate_matches` (unfiltered) for the same candidate
   list. Migrate it the same way to `ChunkResolver::member_candidates` for
   consistency, OR leave it on the matcher until the matcher is deleted — it is a
   read-only query tool, not the minimizer, but it shares the crate and the same
   `source_match::MemberBindingMatch` import, so it must be migrated before the
   matcher symbols can be dropped. Include it in the same change.

5. **Remove now-dead imports/uses** in `selector_codemod.rs`:
   `use source_match::BodyIndexFilter;` (`:22`) and the
   `candidate_index.candidate_set_for_source_match` calls become unused **iff**
   nothing else in the file needs them — check the prove/prefilter soundness
   tests (`prefilter_soundness_tests`, `:2550-2750`) which call
   `member_binding_candidate_matches_within` and `candidate_set_for_source_match`
   directly: those tests assert the _matcher's_ prefilter superset invariant. They
   are matcher-surface tests, not minimizer logic — they can stay until the matcher
   is deleted, or be retargeted to the fact resolver's candidate-list differential.
   Coordinate their fate with the matcher deletion, not this migration.

6. **`source_match::source_match_declared_binding_names` (`:358`)** — used only by
   the `SingleTargetBinding` rewrite (`rewrite_single_target_binding`), **not the
   minimizer**, and it is pure declared-binding extraction from the _selector's own
   source_ (no `AstWildcardMatcher` involvement — `binding_resolution.rs:3` just
   parses + `declared_bindings`). **Leave it as-is**; it is not part of the matcher
   deletion.

## Verification

The minimizer's correctness gate is already a self-contained property test that
re-matches every synthesized selector and asserts unique resolution to the
intended target:

- Property test (`selector_minimizer_proptest.rs`, mounted at
  `selector_codemod.rs:2347`): `minimized_selector_uniquely_matches_target`
  builds random chunks, synthesizes, and re-runs through `matched_body_indices` /
  `synthesize_simplest_selector_for_group`. After the migration these go through
  the fact resolver, so a green run proves end-to-end parity.
- Run:
  - `bbr test //devinfra/js/debundle:selector_codemod_test`
    (the `rust_test` over the `selector_codemod` crate, `BUILD.bazel:690-698`;
    includes the proptest + `full_ast_fallback_tests` +
    `prefilter_soundness_tests` + `adjacent_function_grouping_tests`).
  - Higher proptest coverage:
    `bbr test //devinfra/js/debundle:selector_codemod_test --test_env=PROPTEST_CASES=2000`.
  - Build the crate (lint/clippy ride the build):
    `bbr build //devinfra/js/debundle:selector_codemod`.
- Plus the **prerequisite** candidate-list differential the sibling candidates
  work adds to `//devinfra/js/debundle:corpus_match_differential`
  (`BUILD.bazel:1154`) must be green = 0 disagreements before this migration is
  trusted — that is what proves `member_candidates`' list equals the matcher's
  list over the real tana/re spec.

## Call-site inventory (`file:line` → what it asks)

- `selector_codemod.rs:22` — `use source_match::BodyIndexFilter;` (prefilter type;
  becomes dead after migration unless tests retain it).
- `selector_codemod.rs:358` — `source_match::source_match_declared_binding_names`
  — declared-binding names of a selector source (NOT matcher; `SingleTargetBinding`
  rewrite only; **out of scope, keep**).
- `selector_codemod.rs:1383` — `source_match::resolve_member_binding_group_match`
  — group resolution (categorical, `{body_idx, bindings}`). **→ `ChunkResolver::resolve_member_group` (already serves this).**
- `selector_codemod.rs:1444` — `source_match::member_binding_candidate_matches_within(.., Restricted)`
  — single-target candidate list for the prove gate (count + single match
  `body_idx`/`binding`). **→ `ChunkResolver::member_candidates`.**
- `selector_codemod.rs:1600` — return type `Vec<source_match::MemberBindingMatch>`
  of `matched_binding_candidates`. **→ unchanged type, reuse `MemberBindingMatch`.**
- `selector_codemod.rs:1615` — `source_match::member_binding_candidate_matches_within(.., Restricted)`
  inside `matched_binding_candidates` — the minimizer's core candidate-list call
  (reached by `minimize/object.rs:84`, `minimize/group.rs:211,226`, and
  `matched_body_indices` → `minimize/mod.rs:180` + the proptest). **→ `ChunkResolver::member_candidates`.**
- `selector_codemod.rs:2586` — `source_match::member_binding_candidate_matches_within(.., All)`
  in `prefilter_soundness_tests::brute_force_matches` — **matcher-surface test**,
  asserts the prefilter superset invariant; not minimizer logic (keep with the
  matcher, or retarget at deletion time).
- `minimize/object.rs:84` — `matched_binding_candidates(...)` — trial score
  (`matches.len()`) + `target_unresolved` (`any m.body_idx==decl.body_idx &&
m.binding.binding_name==runtime`). **Indirect; no edit needed.**
- `minimize/group.rs:211` — `matched_binding_candidates(...)` — `slot_resolves`:
  `[m]` unique + `m.body_idx==decl.body_idx && m.binding.binding_name==runtime`.
  **Indirect; no edit.**
- `minimize/group.rs:226` — `matched_binding_candidates(...)` — trial score
  (`target_unresolved`, `matches.len()`). **Indirect; no edit.**
- `minimize/mod.rs:180` — `matched_body_indices(...)` — bare-scaffold uniqueness:
  resolved set `== {decl.body_idx}`. **Indirect; no edit.**
- `match_selector.rs:120,130` — `member_binding_candidate_matches` (unfiltered)
  — candidate list for the read-only `match-selector` query tool (`unique`,
  sorted matches, slack). **Same crate; migrate to `ChunkResolver::member_candidates`
  in the same change (or defer to matcher-deletion).**
- `match_selector.rs:173` — `resolve` closure typed `Vec<source_match::MemberBindingMatch>`
  for slack `[only]` body_idx/binding check. **Follows the `:130` migration.**

## Summary

The minimizer's only real coupling to `AstWildcardMatcher` is the **candidate
list** (`member_binding_candidate_matches_within`, via `matched_binding_candidates`
/ `matched_body_indices`) and the **categorical group resolver**
(`resolve_member_binding_group_match`). The group resolver is already served by
`ChunkResolver::resolve_member_group`. The candidate list is the single gap: it
needs a new `ChunkResolver::member_candidates` returning per-match `(body_idx,
binding)` — the very method the sibling candidates work (F5 step 1) is adding.
The minimizer needs no near-miss / `body_debt` introspection, so it does **not**
couple to the diagnostics surface the other sibling owns. Once `member_candidates`

- its candidate-list differential land, the migration is the three wrapper-body
  swaps above, verified by `selector_codemod_test`.
