# Per-chunk gate against the real minified source (2026-06-18)

First run of the per-target-chunk resolver differential
(`corpus_match_differential`, `PER_CHUNK_JS_ROOT` mode) against the **real**
analysis source — the upstream minified snapshot
`tana/upstream/web/snapshots/78d928dca7/static/<chunk>.js` — rather than the
debundled `js/<chunk>/entry.js` (which for the main chunk is ~700 lines of
`import {…}` and contains none of the code selectors target, so every selector
vacuously rejected).

## Setup

- Spec: `tana/re/web/78d928dca7/spec/modules` — **1751 modules**, 8306 member
  selectors (+ anonymous + binding groups).
- Module→chunk mapping (via emitted `js/<chunk>/<module-path>.js`): **all 1751
  modules map to the single main chunk `index-DI2GynTv`**. This spec debundles
  only the main bundle; the other 53 chunks carry no spec modules. So the
  per-chunk gate is, here, a whole-corpus test against one chunk.
- Resolvers compared per selector: `ChunkResolver` (fact-based, one shared EDB)
  vs `AstWildcardResolver` (production). Production is the oracle.

## Result (budget-limited)

The fact resolver is **~58× slower per selector** than production
(`dl 151.7s` vs `prod 2.6s` over 70 selectors → ~2.17s vs ~37ms each), because
it runs the full structural fact-match against every top-level statement of the
30k-line chunk with **no candidate prefilter**. A full pass is ~5 h, so the
150 s/chunk budget measured only **70 / 8306**:

```
member         resolved-parity 60 | reject-parity 0 | fail-closed 4 | over-resolved 0 | value-disagree 0
anonymous      resolved-parity  4 | reject-parity 0 | fail-closed 0 | over-resolved 0 | value-disagree 0
binding-group  resolved-parity  2 | reject-parity 0 | fail-closed 0 | over-resolved 0 | value-disagree 0
```

- **0 genuine disagreements** (0 over-resolved, 0 value-disagree) among the
  measured selectors.
- **66 same-owner parity** — the fact resolver claims the same owner as
  production. This is the real-owner parity the prior `entry.js` run could not
  show (it was vacuous reject-parity).
- **4 fail-closed** (fact resolver `Err`, production `Ok`) — the residual
  worklist. All are `app/bootstrap/*` function/class declarations.

## The 4 fail-closed — fact-matcher faithfulness gaps on nested bodies

The fact matcher's match **set** diverges from production's for needles that
pin structure _inside_ a function/class body. The capped corpus differential
never exercised these (it matched single statements in isolation, not these
nested-body needles against the chunk's many similar functions).

| Module                                               | Needle shape                                                                         | Fact matcher | Production | Direction   |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------ | ------------ | ---------- | ----------- |
| `AsyncBootloader`                                    | `class … { async load(e){STMT_LIST} async postLoad(e){STMT_LIST} }`, no `CLASS_REST` | 2 matches    | 1 (`a4t`)  | over-match  |
| `boot_progress/versionCheck` `checkForVersionUpdate` | `async fn(ANYTHING){ STMT_LIST; if(ANYTHING===void 0){STMT_LIST} STMT_LIST }`        | 3 matches    | 1 (`Yle`)  | over-match  |
| `initBundle` `resolveOrCreate…`                      | nested-body async fn                                                                 | 11 matches   | 1 (`sqe`)  | over-match  |
| `boot_progress/state` `setBootProgressMessageTarget` | 3 consecutive `function set…(){…}` (multi-statement member)                          | 0 matches    | 1 (`tMt`)  | under-match |

Root-cause hypotheses (to confirm):

- **Over-match (3 cases):** interior `STMT_LIST` run-holes around a pinned inner
  statement, and class bodies with no `CLASS_REST`, are matched too loosely —
  the fact matcher does not faithfully require (a) the pinned interior statement
  in the run, or (b) the absence of extra class members. So functions/classes
  production distinguishes collapse to the same match.
- **Under-match (1 case):** the multi-statement (run) member alignment in the
  fact resolver misses a 3-consecutive-`function` sequence production finds.

These are fail-closed (safe — they error rather than mis-resolve), but they are
**not parity**: a faithful encoding must match production's set exactly.

## Conclusion — revises the prior "switch-ready" claim

The capped/offline runs (matcher differential 0 disagreements; resolver
0-fail-closed-by-shape) were necessary but **not sufficient**: they did not run
the fact matcher against the real chunk's full statement population with
nested-body needles. The real-source gate is the new source of truth, and it is
**not green**. Two prerequisites stand between here and a trustworthy switch:

1. **Candidate prefilter for the fact resolver** (throughput + measurability).
   Without it the corpus pass cannot complete, so corpus-wide parity cannot be
   _proven_ — only spot-checked. Production prefilters per candidate
   (`no_wildcard_shape_prefilter` + the candidate index); the fact resolver
   needs the analog (index `chunk_facts` body items by root node kind, match
   only same-kind candidates — sound because a top-level selector anchors on a
   concrete statement kind).
2. **Faithful nested-body hole matching** (the 4 fail-closed). Interior
   `STMT_LIST` runs around pinned statements, `CLASS_REST` absence, and
   multi-statement run alignment must match production's set exactly.

Neither is a model-level dead end — both are fixable matcher work — but they are
real, and they are the gate to the goal's "parity proven, not asserted." The
fail-closed direction means the current state is _safe_ to shadow (it errors
rather than mis-claims), but not yet correct enough to flip.

## Update — root-kind prefilter landed: sound but insufficient

Implemented the candidate prefilter (commit `954d3525`): `ChunkResolver` caches
each body item's root node kind, and the single-statement scan skips subjects
whose root kind differs from a concrete (non-hole) needle root — the exact
`nkind != subject kind` gate in `homo`, so it changes no verdict.

Re-run, same 150 s budget, head-to-head:

|        | measured  | member r/j/fc/or/vd | dl time | ~per-selector |
| ------ | --------- | ------------------- | ------- | ------------- |
| before | 70 / 8306 | 60/0/4/0/0          | 151.7 s | 2.17 s        |
| after  | 79 / 8306 | 69/0/4/0/0          | 148.9 s | 1.88 s        |

**Confirmed sound** (identical verdicts — same 4 fail-closed, 0 over-resolved, 0
value-disagree; only the measured count rose). But the speedup is **~15 %**, not
the order of magnitude a corpus pass needs (still ~4 h).

**Diagnosis — the dominant cost is not candidate count.** `selector_match::matches`
rebuilds `Index::build(subject)` on **every** call, so the per-`(selector, subject)`
index construction is O(selectors × subjects × subject-size) — billions of
rebuilds across the corpus. The root-kind prefilter only removes the wrong-kind
fraction of those rebuilds; the right-kind subjects (and the var-decl member path,
which scans declarators without going through `matching_body_indices`) still
rebuild per pair. **The real throughput lever is caching each body item's `Index`
once in `ChunkResolver` and threading it into the match path** (sound — a pure
memoization, no semantic change), plus extending the same prefilter/caching to the
var-decl declarator scan. That is the corrected step-2 of the switch plan; the
root-kind prefilter is a necessary-but-partial building block toward it.

## Resolution — both prerequisites landed; gate green over measured

**Throughput (commit `dc1aa695`).** Made `Index` own its labels so `ChunkResolver`
can cache each body item's index once (plus one synthetic single-declarator index
per var-decl declarator), and added prebuilt-index variants of every matcher entry
point. Pure memoization — verdicts unchanged. Per-selector resolve on the main
chunk dropped 1.83 s → 0.34 s (~5.4×); the worst path (declarator-hole members)
6–8 s → 0.71 s. A full 8306-selector pass is now tractable (~50–60 min).

**Faithfulness — the 4 fail-closed were two distinct bugs:**

1. **async/generator dropped from the facts** (commit `a89a58be`). The extractor
   projected every function/arrow to a bare `Function`/`Arrow` node, so
   `async function`≡`function` and the matcher over-matched. Fixed by folding
   async/generator into the node-kind tag (`AsyncFunction`, … / `AsyncArrow`).
   Closed the 3 over-match gaps (AsyncBootloader, checkForVersionUpdate,
   initBundle).
2. **scope-blind alpha bijection** (commit `23414587`). `Bindings` was a single
   flat spelling-keyed map, so a needle reusing a param name across two function
   scopes forced both to the same subject name — under-matching when the subject
   used different param names per function (confirmed: `match_top_level_sequence`
   returned `[[Some(0),Some(1)]]` for matching param spellings but `[]` for
   differing ones). Fixed by making `Bindings` a scope stack mirroring production's
   `alpha_scopes` (references resolve up the stack; bindings shadow in the current
   frame; function/arrow/constructor/setter/catch nodes push a frame). Closed the
   last gap (boot_progress/state).

**Result over the budget-limited prefix.** The gate was green over the 347
selectors a 150 s budget reached: `fail-closed 0 / over-resolved 0 /
value-disagree 0`.

## Correction — the full corpus is NOT green (the prefix was unrepresentative)

The budget-limited runs only ever reached the **alphabetical prefix** of the
modules tree (`app/auth`, `app/bootstrap/*`), which is clean. Running the
**full uncapped 8306-selector pass** (commit `3c9151c9`, parallelized over
modules) tells a different story:

```
member         resolved-parity 6814 | reject-parity 7 | fail-closed 43 | over-resolved 9 | value-disagree 0
anonymous      resolved-parity  619 | reject-parity 0 | fail-closed  0 | over-resolved 0 | value-disagree 0
binding-group  resolved-parity  813 | reject-parity 0 | fail-closed  1 | over-resolved 0 | value-disagree 0
```

So corpus-wide there are **9 genuine over-resolved disagreements** (datalog
resolves a unique owner where production rejects — the matcher is too permissive
on some shape) and **44 fail-closed** (more faithfulness gaps, e.g.
`domains/transcript/pdfExtraction`'s `async function …(ANYTHING)` resolving to 0).
These are **real**, not parallelism artifacts — `production_match_is_globals_independent`
proves the per-task-globals harness does not change production's verdict. The
earlier "switch-ready / gate green" conclusion was **premature**: it generalized
from an unrepresentative prefix. The matcher is **not** at corpus-wide parity.

**Performance.** The full pass is ~137 min single-threaded; dense-`Vec` `Index`
(~3×) + 4-core parallelism (~3.85×) bring it to ~36 min wall — still too slow to
iterate on. The cost concentrates in expensive shapes (declarator-hole members
scanning a giant `initBundle` var-decl owner: per-candidate deep init-homo). The
next lever is **algorithmic** — a candidate index for that path — not more
constant-factor or core-count wins. Only once the pass is fast enough to iterate
can the 44 fail-closed + 9 over-resolved be driven to zero (the actual gate).

## Resolution — gate GREEN over the full 8306-selector corpus (2026-06-19)

The full per-chunk gate is now **green** — every selector resolves to the same
owner as production, zero disagreements, zero fail-closed:

```
member         resolved-parity 6857 | reject-parity 16 | fail-closed 0 | over-resolved 0 | value-disagree 0
anonymous      resolved-parity  619 | reject-parity  0 | fail-closed 0 | over-resolved 0 | value-disagree 0
binding-group  resolved-parity  814 | reject-parity  0 | fail-closed 0 | over-resolved 0 | value-disagree 0
```

Four faithfulness fixes closed the 9 over-resolved + (the prefilter-inflated)
fail-closed worklist. Each was a principled, general encoding fix — no
special-casing:

1. **Member path used gapped alignment, not fixed-window** (the 9 over-resolved).
   `resolve_member_multi` mirrored `find_matching_body_group_alignments` (gapped
   module-level `STMT_LIST` subsequence) when production's member path is
   `find_matching_target_bindings` → `find_matching_body_ranges` (fixed
   `body.windows(n)`). Added `match_fixed_window_sequence_indexed`; a module-level
   `STMT_LIST` member needle now fails closed (production registers `STMT_LIST` in
   `statement_lists`, not the single-statement-wildcard `statements`, so its fixed
   window also finds zero → reject-parity).

2. **Unsound init-kind prefilter for `STR_LITERAL_MATCHING_RE`** (76 false
   fail-closed introduced by the prefilter, never caught until the full pass).
   `needle_var_declarator_init_kind_prefilter` skipped string-literal declarators
   for a regex-predicate needle (a `Call` matching a `StrLit` — a deliberate
   cross-kind match). Added the same regex-predicate carve-out the root-kind
   prefilter already had.

3. **Superclass (`extends`) invisible to the fact matcher** (the `changeTypes`
   ~41-member/group over-match → ambiguous fail-closed). `super_class` was a
   `chunk_facts` relation but `Index`/`homo` never compared it, so a no-`extends`
   needle class matched an `extends`-bearing subject and the "all extend the same
   base" alpha constraint went unenforced. `homo` now compares `super_class` for
   `Class` nodes (production's `match_class` `match_option_box_expr` arm).

4. **`import.meta` / `new.target` unextractable** (the `pdfExtraction` and
   `listElementWithSuspense` under-matches). A subject statement containing a
   `MetaProp` projected to empty facts and was invisible — even when a needle's
   `STMT_LIST`/`DECLARATORS` hole would absorb the offending region (production
   never inspects an absorbed region on the AST, so it matched the owner fine).
   This is the architectural asymmetry: the fact EDB must extract the _whole_
   subject, so extractor coverage gates matchability. `chunk_facts` now extracts
   meta-properties (kind folded into the node tag); the chunk extracts 100% of its
   5316 top-level statements with zero `Unsupported`.

**Reject-parity 16** = selectors where both resolvers error (genuine
non-resolution on this snapshot), which is agreement. The gate counts only
`(Ok,Ok)`-different / `(Ok,Err)` / `(Err,Ok)` as violations; there are none.

Parity is now **proven** over the corpus, the goal's bar. Perf remains the
~36 min/pass cost (Track 1) — fine for a gate, still worth the algorithmic
candidate-index lever before this is a routine pre-commit check.
