# Foreign callback graph reuse

`retain_foreign_callback_effects` repeatedly evaluates a source owner's current
binding roots and traverses the retained callback graph. Different activated
callables can share that source owner and graph. The existing immutable source
census does not reuse this traversal.

The bounded memo retains only `callback_values(roots)`, with exact evaluated roots
and `CallbackGraphReads`. The witness covers property/accessor/descriptor stores,
the escaped pool, reached live cells, and active/inactive namespace cells.
Class promotion precedes every lookup. Native activation, source-owner effects,
current binding availability, object tainting, and the actual callable's heap
ledger still run at every use. The FIFO bank holds at most 128 source-owner
entries; eviction changes cost only.

The shared witness initially snapshots whole persistent stores. Unrelated writes
can therefore prevent reuse. This is neither per-node incremental reachability
nor a cache of callback execution effects.

## Validation

Optimized invocation
[`c385d183`](https://app.buildbuddy.io/invocation/c385d183-36d1-4560-93f3-4777bbc90559)
passed these real-CLI suites:

- `bounded_callback_fallback_heap_test`: 8 cases, including repeated calls after
  nested callback replacement, a captured-cell update, a superclass constructor
  installing a callback, and escaped-pool growth. Existing cases retain distinct
  captures from repeated fresh activations and direct/saved gate behavior.
- `callback_registration_scale_test`: all 4 local/imported, attached/lexical cases.
- `cross_chunk_operation_parity_test`: 33 cases.
- `unknown_call_intermediate_availability_test`: 28 cases.

`retained_callback_factory_test` remained at 21 passing and the same 9 failing
cases as the parent integration baseline. That suite is not green evidence.
Namespace/reexport witness validation is recorded in
[the shared-witness audit](2026_11_15_pooled_effect_overlap.md).

## Registration comparison

The [registration corpus](2026_11_15_callback_registration.md) has no imports.
These single serial runs on the same shared Linux host use default `-c opt`,
`RAYON_NUM_THREADS=4`, and `/usr/bin/time -v`. Projection, analysis and apply all
exited successfully for each case. The table measures analysis only.

| Callbacks | Baseline wall / user CPU | Memo wall / user CPU | Baseline / memo peak RSS, KiB |
| --------: | -----------------------: | -------------------: | ----------------------------: |
|       512 |            0.96 / 0.92 s |        0.98 / 0.94 s |               59,332 / 55,980 |
|     1,024 |            4.01 / 3.92 s |        4.00 / 3.92 s |               77,872 / 74,424 |
|     2,048 |          19.58 / 19.35 s |      20.32 / 20.01 s |             110,404 / 106,368 |

The source-owner memo does not improve this corpus's quadratic trend. It does
not exercise foreign-source promotion. These observations establish neither a
mean speedup nor a canonical Excalidraw improvement.

Baseline CLI SHA-256:
`1f50e6f3f664a35423402306538c4bbfca23ae03227926cc4389f23446c83183`.
Measured memo CLI SHA-256 from the optimized invocation above:
`69080c1cd607ccb6156c6a9a889c583353abbf76bbc417c64408d2b563793b90`.
The later binding-catalogue identity correction is outside this measured binary;
its namespace dependency checks have separate validation.

The same memo CLI's canonical Excalidraw analysis did not complete within a
600-second native limit: wall 600.13 s, user CPU 1,807.54 s, peak RSS 1,723,896 KiB,
exit 124 and no completed analysis artifact. This is a cutoff measurement,
not throughput or canonical acceptance. It uses the same pre-staged projection
and package roots as the prior source-census experiment. The memo alone has not
resolved the canonical cost either.

Input SHA-256, in callback-count order:

- 512: `7e59f1d1ee98d89a7509dab01e54c41d520fa49eff2c53bf55a1b64e54484822`
- 1,024: `686d2d4ee999e046bbf7f16ef4c413e95f523d70571dc04d1479c23836a90c9f`
- 2,048: `4c412fe7a63c66f840dbd2258c7849a2d21bb07cbbad67f2802928e84505c57f`
