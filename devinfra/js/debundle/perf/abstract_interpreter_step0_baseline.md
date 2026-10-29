# Abstract-interpreter step 0 baseline

Measurement-only baseline for <../plans/abstract_interpreter.md> § 5 step 0 and
§ 6.1. No engine, spec or fixture changes. Measured public revision: ducktape
`origin/devel` head `4377a57bae156d0fd2d3ac363a0ad0d71ff6de10` (2026-10-29).
The `-c opt` `//devinfra/js/debundle:debundle` executable and the five scale
test binaries were built in invocation
[fa340938-e854-49a9-83b3-3208495b5ef3](https://app.buildbuddy.io/invocation/fa340938-e854-49a9-83b3-3208495b5ef3);
the measured executable has SHA-256
`d203f5d03ebc9deb895eb81bd1a31073d659755485058f6e11aa952543e04d5b`.

The staging tree that held the captured fixtures, per-run `time.txt`, output
trees, `perf.data` and reports, and the measurement scripts was deleted by a
host `/tmp` cleanup before it was copied to durable storage. What survives is
this note (every number below was read from those files before the deletion),
the BuildBuddy invocations and CAS digests named here, and the procedure, which
is specified closely enough to repeat. Material re-fetched or re-measured later
lives under `/var/cache/agentydragon/debundle-interp-step0/`.

## Public controls

The five e2e scale controls run their fixtures from a temporary directory the
test deletes, so each fixture was captured verbatim: the `-c opt` test binaries
ran with a runfiles shim in place of the engine that copied the fixture root
(`transform_spec.yaml`, snapshot, module tree, empty output root) and then
executed the real engine. Every test passed under the shim; each test function
invoked the engine exactly once, so each case below is one `debundle run
--spec` invocation. The synthetic corpus is `perf/gen_synth_corpus.py --statements
10000 --seed 1 --claim-blocks 0` (10,000 statements, 248 blocks, fully
residual, `inline_in_entry`).

Execution mode for every public-control row: the same `-c opt` executable run
directly on this host (no Bazel, no RBE) under GNU time 1.10 `-v`. Each case ran
six times as three alternating pairs, each run on a fresh copy of the fixture
with the staged root rewritten to the run directory. Host: 32 logical CPUs, 1-minute load
4.1–4.9, about 58 GiB available memory, window 13:38:18–13:39:59 PDT, nothing
else of ours running locally (the canonical runs below executed remotely).

| Control                      | Case                               | Exit | Wall median (s) | Wall min–max (s) | Max pair Δ (s) | User median (s) | Peak RSS median (KiB) | RSS min–max (KiB) |
| ---------------------------- | ---------------------------------- | ---- | --------------: | ---------------: | -------------: | --------------: | --------------------: | ----------------: |
| `opaque_callback_scale`      | bounded_callback_summary_tdz       | 1    |            0.13 |        0.12–0.14 |           0.01 |            0.11 |                54,674 |     54,052–54,992 |
| `opaque_callback_scale`      | cached_callback_summary_later_tdz  | 1    |            0.12 |        0.12–0.13 |           0.01 |            0.12 |                54,542 |     54,096–55,356 |
| `opaque_callback_scale`      | cached_invocations_fallback_fanout | 0    |            2.38 |        2.32–2.43 |           0.11 |            2.30 |               206,112 |   205,784–206,580 |
| `opaque_callback_scale`      | escaped_callback_read_closure      | 0    |            3.17 |        3.08–3.27 |           0.18 |            3.08 |               225,174 |   224,728–225,488 |
| `opaque_callback_scale`      | exported_aliases                   | 0    |            0.46 |        0.45–0.47 |           0.01 |            0.46 |                58,296 |     57,420–59,196 |
| `opaque_callback_scale`      | reentrant_callback_pool            | 0    |            0.14 |        0.13–0.14 |           0.01 |            0.12 |                54,760 |     54,480–55,156 |
| `opaque_callback_scale`      | repeated_opaque_calls              | 0    |            0.21 |        0.20–0.22 |           0.01 |            0.20 |                42,286 |     40,520–42,488 |
| `opaque_callback_scale`      | shared_equivalent_capture_dags     | 0    |            0.04 |        0.04–0.05 |           0.01 |            0.04 |                32,126 |     30,280–32,636 |
| `value_flow_sharing_scale`   | repeated_wide_value_sets           | 0    |            0.10 |        0.09–0.11 |           0.01 |            0.07 |                69,256 |     68,616–69,744 |
| `cross_module_purity_scale`  | namespace_only_provider            | 0    |            0.28 |        0.27–0.33 |           0.06 |            0.26 |               185,652 |   185,400–186,376 |
| `plain_data_escape_scale`    | repeated_writes                    | 0    |            0.23 |        0.21–0.24 |           0.01 |            0.21 |                83,100 |     82,876–83,816 |
| `conflict_diagnostics_scale` | many_independent_conflicts         | 1    |            8.41 |        8.17–9.51 |           1.13 |            8.04 |               978,536 |   977,992–979,572 |
| `gen_synth_corpus`           | 10k statements, seed 1             | 0    |            0.77 |        0.76–0.87 |           0.10 |            0.73 |               158,630 |   157,944–159,680 |

Case names are the test function names shortened; `.rs` sources are in `e2e/`.
Exit 1 rows are rejection fixtures (their tests assert the rejection), measured
like the rest. Within each case all six runs emitted byte-identical JavaScript
(sha256 over sorted relative paths and contents of emitted `.js` files, excluding
reports); the pinned digests are in `results/paired/summary.json`.

Noise: wall pair differences are at most 0.01 s on every case under 0.5 s,
0.06–0.18 s on the 0.3–3.2 s cases, and 1.13 s (13% of the median) on the
8.4 s conflicts case, whose slowest run (9.51 s) is an outlier against five runs
in 8.17–8.57 s. Peak RSS spread is at most 0.5% on every case above 80 MiB and
at most 7.3% (2.4 MiB) on the smallest cases. A later step's comparison on these
controls should therefore treat wall differences under about 15% on the
conflicts case, and under 0.02 s on the sub-second cases, as unresolved.

## Flat profiles

`perf_wrapper.sh` at this revision, `cycles:u`, 99 Hz, DWARF call chains, no
lost samples, reports kept beside `command.sh` under `results/profile-*/profile/`
(`perf_record_stderr.txt`, `perf_report_flat_symbols.txt`,
`perf_report_children.txt`, `perf_report_no_children.txt`, stack slices).

Synthetic corpus (615 samples), self cost:

| Share | Symbol                                                 |
| ----: | ------------------------------------------------------ |
| 11.9% | `__libc_malloc`                                        |
| 11.8% | `__memcmp_evex_movbe`                                  |
|  6.7% | `<swc_atoms::Atom as Ord>::cmp`                        |
|  5.3% | `_int_free_chunk`                                      |
|  4.0% | `js_ast::statement_for_source_unit_ordinal`            |
|  3.9% | `__memmove_avx512_unaligned_erms`                      |
|  3.1% | `ring sha256_block_data_order_hw`                      |
|  2.7% | `IterativeInference<RuntimeRules>::solve`              |
|  2.5% | `drop_glue::<sync_effects::value_model::ValueExpr>`    |
|  1.5% | `RandomState::hash_one::<&(Atom, SyntaxContext)>`      |
|  1.5% | `serde_json::ser::format_escaped_str` (report writing) |
|  1.5% | `BTreeSet<(Atom, SyntaxContext)>::intersection`        |

Conflict-diagnostics control (1,282 samples), self cost:

| Share | Symbol                                                     |
| ----: | ---------------------------------------------------------- |
| 19.7% | `__memcmp_evex_movbe`                                      |
| 12.8% | `<swc_atoms::Atom as Ord>::cmp`                            |
|  4.8% | `<hstr::Atom>::as_str`                                     |
|  4.4% | `js_ast::statement_for_source_unit_ordinal`                |
|  3.4% | `__libc_malloc`                                            |
|  3.3% | `<swc_ecma_ast::ident::Ident>::to_id`                      |
|  3.2% | `BTreeMap<(Atom, SyntaxContext), …>` node handle insertion |
|  2.8% | `SourceUnitReadClosure::new`                               |
|  2.5% | `_int_free_chunk`                                          |
|  2.2% | `IterativeInference<CapabilityContainmentRules>::solve`    |
|  1.7% | `IterativeInference<SccLazyEffectRules>::solve`            |

The plan's § 6.1 "already hot" symbols (cloning `BTreeSet<Id>` and source-owner
sets, copying `BTreeSet<RuntimeKey>`) are not in the top 20 of either flat
profile at this revision; the dominant self cost on both is ordering and
hashing of `(Atom, SyntaxContext)` keys in B-tree maps and sets, plus allocator
churn. These two profiles are the baseline for the "reports them explicitly"
rule in § 6.1.

## Canonical inputs, pinned engine

The private consumer repository's two canonical pipelines ran once each at
`-c opt` on RBE through `bbr`, in invocation
[d9ed8ddb-4889-4ff2-b862-6bb014ff543a](https://app.buildbuddy.io/invocation/d9ed8ddb-4889-4ff2-b862-6bb014ff543a)
(remote runner `8794fe86-c96a-45fb-b2f4-4ebcfa58110e`), with the pipeline
action excluded from the remote action cache so both executed. The engine is
the source-built debundler selected by that repository's pin, public ducktape
`ecb4a7344282e30257a8d96ca6a418a7a64e3ead` (2026-10-27, 617 commits behind the
measured devel head; graph `provenance.engine_sha256`
`9365d228c0ff5b29730bb6c95c39eb320637eb33a0cb6544d188e52a79e78b29`). Execution mode for every canonical row in this note: the `DebundlePipeline`
action executed on a BuildBuddy RBE executor; wall is the executor's
`executionStart`→`executionCompleted` interval and peak memory is
`usageStats.peakMemoryBytes` for that one action, so both cover the engine
process tree only, not Bazel. Canonical and public-control timings are from
different hosts and modes and are never compared with each other. Graph figures are read
from every `dependency_graph.json` the action wrote (one chunk graph for the
web corpus, 76 for the CLI corpus), summed over chunks.

| Input | Verdict | Atomic conflicts (members) | Wall (s) | CPU (s) | Peak RSS (GiB) | Graphs | Source units | Edges | Atomic groups (multi-unit) | Module SCCs (cycles / constrained) | Precision losses |
| ---------------- | ----------------------------------------- | -------: | ------: | -------------: | -----: | -----------: | --------: | -------------------------: | ----------------------------------: | ---------------: |
| Large web corpus | accepted, 2,802 named modules, 0 residual | 0 | 255.2 | 286.2 | 3.67 | 1 | 9,709 | 2,163,669 | 9,263 (93) | 2,631 (41/12) | 11,917 |
| CLI corpus | rejected | 10 (3,003, 1,084, 784, 736, 170, 127, 103, 87, 29, 18) | 43.5 | 89.0 | 4.40 | 76 | 43,949 | 6,123,098 | 34,196 (70) | 132 (43/14) | 44,932 |

Edges by reason (web / CLI): `eager_use` 1,109,226 / 3,176,570; `sequenced`
1,033,882 / 2,888,493; `lazy_use` 19,612 / 57,081; `hoisted_use` 525 / 699;
`local_effect` 317 / 0; `lazy_rebind` 96 / 216; `deferred_rebind` 9 / 25;
`eager_rebind` 2 / 14. Precision losses by reason (web / CLI):
`unknown_call_target` 7,130 / 29,151; `unsupported_operation` 3,993 / 11,427;
`callback_pool_limit` 636 / 3,388; `recursive_invocation` 148 / 449;
`invocation_expansion_limit` 10 / 517. The CLI corpus's main chunk alone
carries 3,862,386 edges and the 3,003-unit conflict group. "Constrained" SCCs
are those the report marks `realizable: false` (they carry constraining module
edges); on the web corpus all 12 are cycles and the gate still accepted the
run, so that flag alone is not a rejection count.

Against the plan's § 6 sizing premise: the web corpus is at the low end of the
quoted 2.3M–5.4M edge range (2.16M) and the CLI corpus is above it (6.12M
summed, 3.86M in one chunk); the web corpus's 255 s / 3.67 GiB on the pinned
engine is far from the 106 s / 3.75 GiB large-web-corpus figure in
<large_web_corpus_baseline.md>, which measured an older engine on a different
spec revision, so that figure should not be used as the step 0 wall baseline.
The pinned engine predates `native_receiver_certificates` (PR #12646), so the
pinned graphs carry no certificate rows and no `inference_uses`; the receiver
count in the next section comes from the devel engine.

## Canonical inputs, measured devel engine

Pending. Both pipelines were started with the same flags on the measured devel
head, selected through an uncommitted `DUCKTAPE_COMMIT` patch, in Bazel
invocation
[b9e9ab47-23d9-46a2-9bf5-d22a1e119f48](https://app.buildbuddy.io/invocation/b9e9ab47-23d9-46a2-9bf5-d22a1e119f48)
(runner `f1f96e04-d89b-4d5e-865c-e2687e628c2c`); the engine was still compiling
when this note was committed. The receiver-certificate count depends on this
run, since only the devel engine emits `native_receiver_certificates`. A
parallel worker's local run of the web corpus on a devel-equivalent engine
reported a rejection with 5 atomic-group conflicts (210, 7, 9, 2, 3 members),
3,146,382 edges, 1,121 s wall and 5.1 GiB peak RSS; that measurement is in a
different execution mode and is quoted here only as the expected shape, not as
a row of the table above.

Private detail (chunk names, conflict members, module paths, the exact
private commit and spec fingerprints) is in the private repository's note
`tana/re/web/debug/2026-10-29-interpreter-step0-baseline.md`.
