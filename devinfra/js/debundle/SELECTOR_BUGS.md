# Selector Bugs And Gaps

Selector issues found while porting a downstream debundle spec, or reported by
code review. Examples are intentionally generic and anonymized. Every entry
carries a **Status**: the date it was last reproduced, or `unreproduced` with how
it was found. An entry is deleted when its fix lands with a test, or when it is
disproved.

## Unknown Hole Keywords Match As Identifiers

Status: open (reproduced 2026-09-24). Nothing checks for unknown hole
keywords, so a hole keyword the binary does not know is a plain free identifier
on every path, including `match-selector`. A selector written for a newer hole vocabulary and run by an
older pinned debundler reports `no_match` or `ambiguous` instead of `invalid`.

The fix is a design decision first: an old binary cannot recognise a keyword it
has never seen, only a name of a reserved shape. <SPEC.md> § Matching rule 1
needs a reservation rule for hole-shaped names that every future hole keyword
falls inside and that real chunk identifiers do not (an all-caps rule would
catch globals such as `JSON` and `URL`). A reserved name the binary does not
implement is then `invalid` with "unsupported selector hole", on every command.

## Regex Predicate Prefix Pruning Can Drop Matches

Status: unreproduced (code review 2026-09-30; the function was read, no selector
was run). `selectors/matching/selector_match.rs` `regex_required_literal_prefix` pushes each literal
character of a `^literal...` pattern up to the first metacharacter, so a
quantifier that binds the last pushed character is ignored: `^foo*` yields the
prefix `foo` but matches `fo` and `fox`; `^ab?` yields `ab` but matches `ac`.
`chunk_resolver` range-scans the predicate postings by that prefix, so values the
regex matches are pruned before matching, and a selector can resolve to one place
when the regex matches two. No unit test covers the function.

Fix: drop the last literal character when the next one is `*`, `?` or `{`, and
test each quantifier form.

## Invalid Regex Predicate Matches Nothing

Status: unreproduced (code review 2026-09-30, not verified).
`selectors/matching/selector_match.rs` compiles a `STR_LITERAL_MATCHING_RE` pattern with
`if let Ok(compiled) = Regex::new(pattern)`, so a pattern that fails to compile
makes the predicate match nothing with no diagnostic (a silent fallback, STYLE.md
§ General, Exceptions). Not checked: whether `selectors/matching/source_match/parse_validate.rs`
rejects an invalid pattern earlier. If it does not, reject it there as `invalid`.

## Hole Keyword Sets Can Drift

Status: unreproduced (code review 2026-09-30). Hole-ness is implemented three
times: AST-level in `selectors/matching/source_match/holes.rs`, fact-level in `selectors/matching/selector_match.rs`,
and a local `is_hole_keyword` in `selectors/authoring/match_selector.rs`, kept in sync by "mirrors"
comments. The `selectors/authoring/match_selector.rs` copy omits `ARRAY_ELEMENTS`,
unlike `selectors/matching/selector_match.rs`. No input was found where this changes output. Fix: one
`is_hole_keyword` in `selectors/matching/source_match_holes.rs`.

## Synthesized-Selector Replacement Count Can Under-Report

Status: unreproduced (code review 2026-09-30; `holes_present` was read, the
renderer's `ARGS`/`CASE_REST` output was not re-checked). `selectors/authoring/render.rs`
`holes_present` tests for only `ANYTHING`, `STMT_LIST` and `DECLARATORS`, while
the renderer also emits `ARGS` and `CASE_REST`. `rewritten_holes`, and the
`replacement_count` that `selectors/authoring/selector_codemod.rs` derives from
`rewritten_holes.len()`, would then omit them. Fix: one keyword list shared with
the renderer, and a test that a selector holing each keyword reports it.

## Solver Domain Encoding Ignores Id Gaps

Status: unreproduced (code review 2026-09-30, not verified).
`selectors/resolution/selector_constraint_backend.rs` `ensure_full_domain_contains` returns silently
when `usize::try_from` fails or when `index > values.len()`, and appends only when
`index == values.len()`. A gap in encoded ids drops the value from the full
domain instead of raising. Fix: treat both branches as invariant violations.

## Too-Broad Count Can Overstate Places

Status: unreproduced (code review 2026-09-30, not verified). `Rejection::check_count`
in `selectors/resolution/selector_resolve.rs` caps and reports `rows.len()`, but `narrow_by_references`
calls it on collected rows that `project_collected` dedupes only later (`distinct`),
and rows can repeat one place with different `free_bindings`. The `too_broad`
message "matches N places" can overcount. Fix: count distinct places.
