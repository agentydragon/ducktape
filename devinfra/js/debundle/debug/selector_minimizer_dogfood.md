# Dogfood: read-off minimizer on the real tana/re spec

Real-chunk measurements of the read-off `synthesize-selectors` minimizer against
the ~7 MB minified chunk (`static/index-DI2GynTv.js`). Earlier search-era and
migration-phase findings (the apply-crash fix, the original over-pin shapes) are
superseded; next steps live in the plan backlog
(<../plans/readoff_minimization.md>). What remains here is the load-bearing perf
measurement that backs the W4 budget item, plus the current dogfood-apply survey
(below) that drives the next over-pin-reduction wave.

## Dogfood-apply survey (2026-06-17, post enclosing-context + interior-cover)

Whole-spec `synthesize-selectors --apply` (`-c opt` binary, gaffer
`tana/re/web/78d928dca7` spec, the unmodified name-pinned spec). Measures what the
**current** minimizer — with the post-#2291 capabilities landed
(#2310 wide-destructure, #2315 enclosing-context anchoring, #2318 callee/arg
holing, #2289 interior cover, #2306 multi-target binding-group read-off) — does on
the real spec, and where it still over-pins.

| metric                                   | value           |
| ---------------------------------------- | --------------- |
| modules / members scanned                | 1745 / 5903     |
| fragile `binding.name` members           | 2227            |
| **converted** (`would_change`)           | **1104 (~50%)** |
| selectors emitted (incl. binding groups) | 1013            |
| skipped (no sparse selector)             | 1123            |
| wall (`--apply`, whole spec, `-c opt`)   | **13.1 s**      |

The **~50% conversion** is a large jump from the ~9%-convertible the plan recorded
at gaffer `main` after #360 (then: ~91% "no sparse selector"). The
enclosing-context anchoring (#2315) and interior cover (#2289) recover most of the
previously-skipped whole-body-only tail — but they are also where the new over-pin
debt comes from (below). The ~1123 still-skipped are the residual hard tail.

### Converted-selector size distribution (1013 emitted)

`match`-block line count: min 1, median 13, mean 27.4, max 904.

| ≤10 | 11–20 | 21–40 | 41–100 | >100 |
| --- | ----- | ----- | ------ | ---- |
| 414 | 251   | 191   | 117    | 40   |

Two-thirds (656/1013) land ≤20 lines — compact, robust selectors. The >40-line tail
(157) is where the over-pin lives.

### Over-pin survey (operational rule: `match` >40 lines AND ≤2 holes)

**62 hard over-pins** (a further 95 are >40 lines but well-holed, kept). Classified
by shape:

| n   | shape                                                                    |
| --- | ------------------------------------------------------------------------ |
| 46  | **neighbor-context anchoring pins the whole neighbor declaration**       |
| 11  | function / class whole-body (interior cover found no compact anchor)     |
| 2   | **class-expression-valued `const X = class {…}` pinned whole (0 holes)** |
| 2   | object-literal whole / async-generator parse edge                        |

The two **bolded** shapes are clean, fixable gaps with no existing coverage, so
each is captured as a new ignored expectation case in
`e2e/selector_minimizer_expectation_test.rs` (anonymized minimal reproductions):

- **`neighbor_context_whole_function_neighbor`** — #2315 picks the right stable
  neighbor but, when it is a function _declaration_ (not a single call statement),
  pins its body verbatim instead of running it back through the per-form read-off.
  Dominant shape (46/62). Real analogues: the `SubscriptionFlow` /
  `nodeDisplayName` wrappers whose preceding ~40-line component function is kept
  whole; the largest is `features/search/popoverState.yaml::$rt` at **904 lines /
  1 hole**.
- **`class_expression_const_whole_body`** — `try_var_read_off`'s `hole_expr` has no
  `Expr::Class` arm, so a class-expression initializer never reaches the class
  read-off (CLASS*REST holing). The equivalent class \_declaration* minimizes
  correctly (control verified). Real analogues:
  `integrations/google/api/client.yaml::GoogleApiClient` (314 lines, 0 holes) and
  `features/search/state.yaml::SearchState` (300 lines, 0 holes).

The 11 function/class whole-body cases are the known structural tail the interior
cover (#2289) explicitly leaves open (uniqueness genuinely needs ~the whole body);
they are not new gaps. Per the operational rule, the 62 hard over-pins revert to
name pins in the dogfood-apply PR; the well-holed >40-line conversions ship.

## Read-off minimizer real-chunk perf (2026-06-16, post prove-gate-via-index)

W4 perf acceptance measurement of the **current read-off minimizer** (shape-index
`minimal_anchor_set` + `kept_spans_for_anchor_set`, the default minimization path;
no `--no-minimize`), **with the prove-gate-via-index fast-path (#2280)**. Dry run
(no `--apply`), `name-binding-to-source-match` rewrite, **`-c opt` binary** run
directly (no Bazel overhead), wall-clock via `time.perf_counter`, peak RSS via
`getrusage(RUSAGE_CHILDREN)` on a fresh child per scope. Members counted as
`name_binding_members` (the members the minimizer actually processes). Chunk:
`static/index-DI2GynTv.js`.

| Scope           | members | seconds       | peak RSS    |
| --------------- | ------- | ------------- | ----------- |
| infra           | 155     | 2.0           | ~242 MB     |
| integrations    | 106     | 2.9           | ~242 MB     |
| shared          | 247     | 1.8           | ~242 MB     |
| app             | 940     | 4.0           | ~242 MB     |
| domains         | 1082    | 3.8           | ~242 MB     |
| features        | 1976    | 7.5           | ~242 MB     |
| **whole chunk** | 4506    | **13.0–13.6** | **~243 MB** |

(Whole-chunk timed twice: 13.0s and 13.6s — stable around ~13s. Peak RSS is flat
at ~242 MB across every scope: the parsed 7 MB AST + indices dominate and the
per-member work allocates little, so memory is not scope-sensitive.)

- **Index/parse floor ≈ 1.2s.** A scope whose prefix matches zero files (0 members
  processed) pays ~1.2s — parse the chunk once, build the candidate index and the
  read-off shape index. This one-time cost dominates the small scopes.
- **Per-member cost ≈ 0.0027 s/member** on the whole chunk
  (`(13.3 − 1.2) / 4506`), down from ~0.024 s/member before #2280 — a ~9× drop.
  Whole chunk = floor + members × ~0.0027s.

### Budget verdict

- **Meets the ≤30s hard budget on the whole chunk with margin** (~13s, ~2.3× under
  the cap) and **narrowly misses the ≤10s ideal** (~13s vs 10s).
- **Every sub-scope is well within budget**: the largest single subtree
  (`features`, ~2k members) is ~7.5s, and scope-at-a-time review is effectively
  instant. The whole-chunk hard-budget acceptance criterion is now met; closing the
  last ~3s to the ideal would come from the parse/index floor or batching the
  residual non-singleton proofs.

### Where the time goes

The read-off layer makes the **selector choice** cheap (shape-index anchor set, no
full-AST scan). The prove-gate-via-index fast-path (#2280) then collapses the
per-member **uniqueness proof**: `prove_synthesized_selector` restricts the
production matcher to the candidate-index posting-list intersection, so for the
singleton-provable majority (the `OPT=1` common case) the matcher inspects a single
item instead of scanning the matching siblings. The split is now **~1.2s one-time
build** + **~0.0027 s/member**, so neither phase dominates and the whole chunk lands
near the parse/index floor plus cheap per-member work.

### Comparison to prior numbers

- **vs. the ~110s pre-#2280 read-off baseline** (and the ~113s search-era baseline):
  whole-chunk wall-clock drops from **~110s to ~13s (~8.5×)**. Before #2280 the
  prove-gate (full matcher, once per member) was essentially the entire cost
  (~0.024 s/member × ~4.5k ≈ 107s); restricting it to the index intersection removed
  that bottleneck, which is where the speedup comes from. `features` alone went
  57.6s → 7.5s, `app`/`domains` 27s → ~4s.
- **vs. the synthetic `OPT=1` = 100% prediction:** now borne out on the real chunk —
  the candidate-index intersection is a singleton `{target}` for the overwhelming
  majority of members, so the matcher proves uniqueness by inspecting one item and
  the residual per-member cost is negligible.

## Index-build perf (#2291, roaring-bitmap posting lists + `FxHashMap`)

Same `-c opt` whole-chunk dry-run as above, on the **current** spec (the
dogfood-apply backlog has since converted ~half the name-pins, so the spec now
reports **2,227** `name_binding_members`, not the 4,506 of the table above —
fewer members to minimize, so even the unmodified binary is already under the
≤10s ideal). To isolate the data-structure change from the spec-size change,
binaries were built `-c opt` and run back-to-back on this same spec
(`static/index-DI2GynTv.js`, `time.perf_counter` × 3, `RUSAGE_CHILDREN`):

| binary                           | whole chunk (median) | best  | peak RSS |
| -------------------------------- | -------------------- | ----- | -------- |
| baseline (`BTreeMap`/`BTreeSet`) | 8.2 s                | 7.4 s | ~243 MB  |
| sorted `Vec<u32>` (intermediate) | 7.0 s                | 6.6 s | ~222 MB  |
| **roaring (shipped)**            | **7.3 s**            | 6.9 s | ~243 MB  |

The shipped change (roaring) is ≈ **11% off the whole-chunk wall-clock** vs the
baseline; the largest single scope (`features`, 1,037 members both runs) drops
4.0 s → 3.65 s best. The isolated index-build + read-off lever is larger (≈1.8×
build / 4.5× read-off on the synthetic sweep — see <selector_minimizer_perf.md>);
on the real chunk the fixed swc parse of the 7 MB chunk and the per-member
render/prove work dilute it.

Roaring is the standard posting-list structure and its read-off intersection beat
a hand-rolled sorted-`Vec` merge in the synthetic sweep. On this **selective-heavy
real chunk** (most posting lists are size 1–2) a bespoke `Vec<u32>` would have been
~21 MB leaner (the intermediate row) — roaring's per-container overhead keeps the
footprint flat with the baseline rather than improving it. That memory delta was
the accepted tradeoff for using the vetted structure; wall-clock and the ≤10s ideal
are unaffected.
