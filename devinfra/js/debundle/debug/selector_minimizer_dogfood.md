# Dogfood: read-off minimizer on the real tana/re spec

Real-chunk measurements of the read-off `synthesize-selectors` minimizer against
the ~7 MB minified chunk / ~4.5k name-pin members. Earlier search-era and
migration-phase findings (conversion rates, the apply-crash fix, the over-pin
shapes) are superseded: the over-pin patterns are now tracked as the disabled E2E
expectation cases, and next steps live in the plan backlog
(<../plans/readoff_minimization.md>). What remains here is the load-bearing perf
measurement that backs the W4 budget item.

## Read-off minimizer real-chunk perf (2026-06-16)

W4 perf acceptance measurement of the **current read-off minimizer** (shape-index
`minimal_anchor_set` + `kept_spans_for_anchor_set`, the default minimization path;
no `--no-minimize`). Dry run (no `--apply`), `name-binding-to-source-match`
rewrite, binary run directly (no Bazel overhead), wall-clock via `time.perf_counter`,
peak RSS via `getrusage(RUSAGE_CHILDREN)` on a fresh child per scope. Members
counted as `name_binding_members` (the members the minimizer actually processes).

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
  parse floor is amortised. Whole chunk = floor + members × ~0.024s.

### Budget verdict

- **Misses the ≤10s ideal and the ≤30s hard budget on the whole chunk by a wide
  margin** (~110s — ~3.7× over the 30s cap, ~11× over the 10s ideal).
- **Sub-scopes do meet budget** except the largest: `infra`/`integrations`/
  `shared`/`app`/`domains` ≤30s; `features` (~58s, ~2k members) is the only single
  subtree over the hard cap. Scope-at-a-time review stays in budget except for the
  largest subtree; the whole-chunk acceptance criterion is not met.

### Where the time goes

The read-off layer makes the **selector choice** cheap (shape-index anchor set, no
full-AST scan), but it does not remove the per-member **uniqueness proof**:
`synthesize_simplest_selector_for_group` still calls `prove_synthesized_selector`
(`source_match::resolve_member_binding{,_group_match}` — the production matcher)
for every emitted selector, over the candidate-index-filtered AST. So the split is
**~3.3s one-time build** + **~0.024 s/member prove-gate**, the prove-gate
accounting for essentially all ~107s of per-member time on the whole chunk.

### Comparison to prior numbers

- **vs. the 113s search-based baseline:** whole-chunk wall-clock is **unchanged**
  (~110s vs 113s). Read-off made the _selector-synthesis_ phase cheaper, but the
  prove-gate (full matcher, once per member) was already the bottleneck and is
  untouched, so total wall-clock did not move. **The read-off work optimised the
  cheap half; the prove-gate is now the entire cost.**
- **vs. the synthetic `OPT=1` = 100% prediction:** the candidate-index prefilter
  collapses each _matcher call_ from O(all top-level statements) to O(matching
  siblings) — that holds — but it does not eliminate the once-per-member proof:
  residual ~0.024 s/member × ~4.5k ≈ 107s still dominates. Hitting budget needs the
  proof itself amortised/batched across members, or the prove-gate restricted to a
  cheaper equivalence check the read-off shape index can discharge for the common
  single-proven-unique case (the planned "prove-gate via index" fix).
