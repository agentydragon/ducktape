# Dogfood: read-off minimizer on the real tana/re spec

Real-chunk measurements of the read-off `synthesize-selectors` minimizer against
the ~7 MB minified chunk / ~4.5k name-pin members. Earlier search-era and
migration-phase findings (conversion rates, the apply-crash fix, the over-pin
shapes) are superseded: the over-pin patterns are now tracked as the disabled E2E
expectation cases, and next steps live in the plan backlog
(<../plans/readoff_minimization.md>). What remains here is the load-bearing perf
measurement that backs the W4 budget item.

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

## Index-build perf (#2291, posting lists as sorted `Vec<u32>` + `FxHashMap`)

Same `-c opt` whole-chunk dry-run as above, on the **current** spec (the
dogfood-apply backlog has since converted ~half the name-pins, so the spec now
reports **2,227** `name_binding_members`, not the 4,506 of the table above —
fewer members to minimize, so even the unmodified binary is already under the
≤10s ideal). To isolate the data-structure change from the spec-size change,
both binaries were built `-c opt` and run back-to-back on this same spec
(`static/index-DI2GynTv.js`, `time.perf_counter` × 3, `RUSAGE_CHILDREN`):

| binary                           | whole chunk (median) | best  | peak RSS |
| -------------------------------- | -------------------- | ----- | -------- |
| before (`BTreeMap`/`BTreeSet`)   | 8.2 s                | 7.4 s | ~243 MB  |
| after (`FxHashMap`/sorted `Vec`) | 7.0 s                | 6.6 s | ~222 MB  |

≈ **15% wall-clock** and **~21 MB peak RSS** off the whole chunk (the smaller
`Vec<u32>` posting lists replace the many small `BTreeSet` nodes). The largest
single scope (`features`, 1,037 members both runs) drops 4.0 s → 3.7 s best. The
isolated index-build + read-off lever is larger (≈1.9× build / 3.7× read-off on
the synthetic sweep — see <selector_minimizer_perf.md>); on the real chunk the
fixed swc parse of the 7 MB chunk and the per-member render/prove work dilute it,
but both phases the change touches got materially cheaper and the ≤10s ideal is
met with margin.
