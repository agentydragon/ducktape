# Fact-based selector resolver performance

Measured profile of the post-#2398 fact-based selector resolver — the
`source_match::ChunkResolver` (`datalog_resolver.rs`) that became the sole
`SelectorResolver` when the AST matcher was deleted. It builds a per-chunk
`chunk_facts` EDB once and resolves every `source_match` selector against it
via the `selector_match` structural homomorphism.

This note is the measured baseline for that path. The proposer/gate path lives
in <proposer.md>; the older declaration-hole `source_match` micro-profile lives
in <source_match_selector_profile.md>.

## Budget

Interactive commands target under 10s on warmed inputs; sustained runs over 60s
on the largest known downstream specs are priority bugs unless the command is an
explicit offline/profile mode (AGENTS.md / TODO.md). The largest known
downstream chunk is ~6.9 MiB / 204k lines (<source_match_selector_profile.md> →
"Filter Latency").

## Workload (2026-06-21)

**Why not the documented default.** AGENTS.md → "Performance Profiling" points at
the `debundle_pipeline_with_profiles` sibling targets over the
`props/frontend/debundle/` corpus. At HEAD `05572738` (the #2398 commit) neither
exists yet: `pipeline.bzl` defines only `debundle_pipeline` (no
`_with_profiles`), and there is no `props/frontend/debundle/` corpus. The gaffer
`tana/re/web/78d928dca7` spec was off-limits (a concurrent lane worker was
editing it). So this run uses the reproducible public stand-in the proposer note
already established — the `gen_synth_corpus.py` corpus (gaffer-scale graph shape)
— with one rewrite to make it exercise the fact resolver.

**Corpus.** `gen_synth_corpus.py --statements 10000 --seed 1 --claim-blocks 62`
→ a single 10051-statement / 436 KB chunk, 62 claimed modules, **2461 member
selectors**. `chunk_facts_coverage` fully extracts 100% of the 10051 top-level
statements (zero fail-closed `Unsupported`), so the EDB projection is complete
on this shape.

**The rewrite.** The stock corpus pins members with `binding: {name}` selectors,
which resolve through a cheap name lookup and never touch the fact resolver. A
small harness (`debug/perf/2026_06_21_fact_resolver_source_match/make_source_match_spec.py`)
rewrites all 2461 members to `source_match` selectors (`match` = the declaration
line, `identifiers: exact`, `target_binding` = the binding), so every member
resolution flows through `ChunkResolver::resolve_member` →
`selector_match::matches_indexed`. Output is byte-identical to the binding-name
run (same 12 MB owner graph) — the rewrite changes only the resolution path, not
the result.

**Representativeness caveat.** 436 KB / 10k statements is the same _shape class_
as a real bundle but ~15x smaller than the largest known downstream chunk, and
the synthetic statements are simpler (one declaration per line, no minified
vendor soup), so a real chunk will hit more `selector_match` `Unsupported`
fall-backs and longer needles. Treat the absolute numbers as a lower bound on a
comparably-selector-dense real spec; the scaling result below is the load-bearing
finding.

## Absolute elapsed (warmed, `-c opt`)

Binary built `-c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1`. Wall is
median of ≥3 warmed runs.

| Command                             | binding-name selectors | source_match selectors |
| ----------------------------------- | ---------------------: | ---------------------: |
| `run --spec` (10k corpus)           |                  0.43s |                   4.4s |
| `spec validate --spec` (10k corpus) |                  0.30s |                   4.4s |
| `spec match-selector` (1 selector)  |                      — |      0.10s (alpha-all) |

`match-selector` (one selector against the full 10051-statement chunk) is well
inside budget: it builds the EDB once and matches a single needle. The whole-spec
commands pay that match 2461 times.

### Scaling — the headline

`run --spec` with source_match selectors, scaling statements/selectors:

| Statements | Chunk size | Claimed selectors | Warmed wall |
| ---------: | ---------: | ----------------: | ----------: |
|        10k |     436 KB |              2461 |        4.4s |
|        40k |    1.88 MB |             10017 |        ~67s |

4.07x the selectors → **~15x** the wall. The path is **super-linear (≈O(n²))** in
selector/statement count: each of N selectors scans O(N) candidate top-level
statements. At 40k statements the whole-spec resolve already **breaches the 60s
blocker threshold**. The largest known downstream chunk (6.9 MiB / 204k lines) is
~3.7x the 40k corpus again; a comparably selector-dense real spec on it would be
far past the blocker.

## Fix landed (2026-06-21) — both directions, quadratic killed

Two behavior-preserving changes (byte-identical debundle output — the emitted JS
tree `diff -r`s identically at 10k and 40k; the only `modules.json` delta is the
self-reported `nanos`/`secs` timing fields; the full 132-test suite + e2e stay
green):

1. **Hoist per-needle validation out of the candidate loop.**
   `matches_prepared` / `var_declarator_alignment_prepared` skip the needle-only
   `unsupported_needle_construct` faithful-subset check (three needle-index passes
   - `collect_subtree` recursion) that was re-run per candidate; the resolver
     probes the needle once up front, before the candidate loop, so skipping it
     per-candidate is sound. Removes the dominant self-cost (the profile's 15%
     self / 38% inclusive entry).
2. **Exact-mode identifier candidate index (kills the quadratic).** The existing
   per-chunk token postings index (`body_tokens` / `declarator_tokens`) only keyed
   on literals/property-names/regex, so an **identifier-only, exact-mode** needle
   (`const NAME = a + b;` — the dominant corpus shape, and the realistic
   minified-name exact pin) pinned **no** token and fell back to the full O(N)
   candidate scan → O(selectors × statements). New `Token::Ident`: subjects are
   indexed (`subject_tokens`) by every identifier spelling; a needle in
   `Mode::Exact` **without** a `target_binding` prebind requires its non-hole,
   non-absorbed identifier spellings (`needle_required_tokens`), so the postings
   intersection prunes to the few structurally-compatible candidates. Soundness:
   in exact mode `homo` compares those identifiers byte-for-byte
   (`Bindings::resolve_unbound` ⟹ `needle == subject`), so any real match carries
   them — the prune is over-inclusive, never under-inclusive. The prebind paths
   (declarator-hole, which alpha-couples the target name to a candidate's) pass
   `allow_exact_ident = false` and keep the literal-only candidate set; alpha-mode
   needles never query an `Ident` token, so their behavior is unchanged.

Warmed wall (`-c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1`, median of 3,
4-core VM — so the absolute baseline is a touch slower than the original ~67s
measurement, but the **scaling** is the load-bearing result):

| Statements | Selectors | Baseline | +#1 (hoist) | +#1+#2 (ident index) |
| ---------: | --------: | -------: | ----------: | -------------------: |
|        10k |      2462 |    4.25s |       3.12s |            **0.45s** |
|        40k |     10018 |    80.3s |       42.7s |            **2.76s** |

4.07x selectors → **6.2x** wall after the fix (scaling exponent
log 6.2 / log 4.07 ≈ **1.30**, down from ≈2.1). The quadratic is gone — the
remaining slope is the near-linear EDB build + O(matches) work, not the
all-candidates scan. **Verdict: both interactive (<10s) and blocker (<60s) budgets
are met with wide margin at 40k.** Extrapolating the 1.3 exponent, the 6.9 MiB /
204k-line chunk (~5x the 40k statement count, comparably selector-dense) lands
on the order of ~25s — inside the 60s blocker, though a follow-up re-measure on a
real chunk is warranted (the synthetic statements are simpler; see the caveat
above). Re-profile recipe unchanged:
`debug/perf/2026_06_21_fact_resolver_source_match/command.sh`.

## Call-graph profile (callgrind, Ir-only)

`perf` is unavailable in this environment, so the call-graph profile uses
`valgrind --tool=callgrind` (Ir-only, `--cache-sim=no --branch-sim=no`) — the
same recipe the prior whole-spec callgrind run used
(`debug/perf/2026_06_17_dogfood_apply_whole_spec/command.sh`) and the right tool
for the "exact call counts + call graph" intent. Target: `run --spec` over the
10k source_match corpus. Total: 34.95 billion Ir. Artifacts:
`debug/perf/2026_06_21_fact_resolver_source_match/`.

Inclusive (call-graph) spine:

```text
pipeline::run_transform_cli_with_options                       99.24%
└─ lowering::materialize_logical_modules                       96.82%
   └─ materialize::materialize_logical_chunk                   96.10%
      └─ ChunkPlanBuilder::add_explicit_request                92.18%
         └─ ChunkResolver::resolve_member                      92.09%  ← fact resolver
            └─ selector_match::matches_indexed                 89.72%  ← homomorphism
               └─ datalog_resolver::member_matches_var_declarator  88.75%
                  ├─ selector_match::homo                      42.50%
                  ├─ selector_match::unsupported_needle_construct  38.33%
                  └─ selector_match::homo'2                    35.09%
```

Top self (exclusive) costs:

| Self % | Function                                        |
| -----: | ----------------------------------------------- |
| 15.12% | `selector_match::unsupported_needle_construct`  |
| 12.14% | `selector_match::homo'2`                        |
| 11.03% | `selector_match::is_run_hole_carrier`           |
|  9.94% | `__memcmp_avx2_movbe` (libc — exact-id compare) |
|  9.61% | `source_match_holes::hole_name_for`             |
|  6.36% | `selector_match::homo`                          |
|  4.63% | `selector_match::shorthand_property_view`       |
|  3.26% | `selector_match::match_children`                |
|  ~6.0% | libc `malloc`/`free`/`_int_*` (matcher churn)   |

Read-out:

- **The fact-based resolver IS the hot path** — ~92% of the whole pipeline on a
  source_match-selector spec. `selector_match` (the homomorphism matcher) is
  ~90%.
- **`chunk_facts` is NOT hot.** The EDB projection
  (`chunk_facts::extract_facts`, `selector_match::Index::build` at 0.08% self) is
  a cheap once-per-chunk build and never surfaces in the top inclusive list. The
  cost is per-selector _matching_, not fact extraction.
- **`selector_solve` / the Ascent datalog kernel does not appear at all.** It is
  the separate Phase-1 shadow / X-primitive solver (the `selector-solve`
  subcommand and the shadow gate), not on the `run` / `validate` path. The "fact
  resolver" that is hot is `datalog_resolver` + `selector_match`, not the Ascent
  IDB.
- **Known suspects (`JsChunk` linear scans, `split_entry_body` clone) are not
  hot** on this workload — they are below threshold, dwarfed by the matcher.

### Root cause of the dominant self cost

`unsupported_needle_construct` (15.12% self / 38.33% inclusive) is a
faithful-subset guard: three full passes over the needle index plus
`collect_subtree` recursion and `HashSet<NodeId>` allocations. It is a property
of the **needle alone**, but every entry point
(`var_declarator_alignment_indexed`, `matches`, `matches_indexed`) re-runs it on
**every candidate alignment** — so for N selectors × O(N) candidates it is
recomputed O(N²) times for inputs that never change between candidates.
`is_run_hole_carrier` (11.03%) and `hole_name_for` (9.61% — the hole-keyword
string dispatch) are the same story: needle-classification work re-derived per
candidate. This is exactly the "prepared selector reuse / memoized selector-body
resolution" the TODO P2 item already names.

## Recommendations

Ordered by leverage. Validate each against a fresh callgrind run on this
workload; treat a change as successful only if it lowers whole-spec wall or the
super-linear slope, not just a microbenchmark.

1. **Hoist per-needle validation/classification out of the candidate loop.**
   Compute `unsupported_needle_construct` and the run-hole-carrier /
   `hole_name_for` classification **once per prepared selector** (cache on the
   parsed needle `Index`), then have the candidate inner loop consult the cached
   result. This alone targets ~35–40% of the profile (the three top self entries
   are all needle-only work re-run per candidate). Highest value, lowest risk —
   it is a pure memoization with no semantics change.
2. **Cut the O(N) candidate scan to O(matches).** `member_matches_var_declarator`
   scans all top-level statements per selector. Build the per-chunk candidate
   index (declaration kind + var kind/declarator count + cheap literal/ident
   fingerprint) once and intersect postings before recursive matching, so each
   selector touches only plausible candidates. This is the shared per-chunk
   source-match index the TODO P2 #1 and <source_match_selector_profile.md> →
   "Remaining Work" already call for; the partial `intersect_postings` /
   `matching_body_indices` machinery (0.12% / 3.21%) is the seam to extend. This
   is what removes the quadratic.
3. **Faster exact-identifier compares.** `__memcmp_avx2_movbe` at 9.94% is exact
   string comparison from `identifiers: exact`. Interning identifier atoms in the
   EDB and comparing atom ids would drop it; lower priority than 1–2 and partly
   subsumed once the candidate set shrinks.

Do not add stage-timing fields to production code for this — the call-graph
profile already attributes it. Keep production telemetry coarse (AGENTS.md).
