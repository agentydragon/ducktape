# Imported binding identity lookup

`program_binding_target` resolves a linker's source-local spelling to the exact
SWC binding identity. Scanning every top-level binding for each imported read
makes this lookup depend on the provider's declaration count. The old scan
requires exactly one match; it refuses both missing names and duplicate
spellings with different hygiene identities.

The immutable source view now indexes that same set by spelling. An absent key
means missing, and a present `None` means ambiguous. Iteration order cannot
choose a winner. This index supplies identity only: values, initialization,
`var` readiness, anonymous default cells, namespace completeness and live
reexports still use the existing module-cell paths.

## Validation and measurement

Optimized invocation
[`194c2fec`](https://app.buildbuddy.io/invocation/194c2fec-d506-4493-9cb6-8cab77c9eab7)
passed `callback_registration_scale_test`, `cross_chunk_operation_parity_test`,
`namespace_live_carrier_test`, `evaluation_schedule_test`, and
`saved_analysis_parity_test`. The initial build failed because SWC's `Atom` does
not implement `Borrow<str>`; the index uses string keys in the validated build.

The import corpus has a provider containing `export const valueN = N;` for
512, 2,048 or 8,192 declarations. Its importer names `value0` through `value63`,
then declares 128 results, each summing those 64 bindings, and logs the first
and last result. Both chunks remain in their residual entries, with no author
annotations. Projection, analysis and apply succeeded; original and emitted
Node output was `2016 2016` for every run.

These serial native measurements use `-c opt`, `RAYON_NUM_THREADS=4` and
`/usr/bin/time -v` on the same shared Linux host. The repeat reverses the binary
order. They measure analysis only, not queue/setup or projection/apply.

| Provider declarations | Baseline wall / user CPU | Candidate wall / user CPU | Repeat baseline / candidate wall |
| --------------------: | -----------------------: | ------------------------: | -------------------------------: |
|                   512 |            0.24 / 0.22 s |             0.22 / 0.19 s |                    0.26 / 0.21 s |
|                 2,048 |            0.46 / 0.45 s |             0.34 / 0.31 s |                    0.41 / 0.33 s |
|                 8,192 |            1.24 / 1.29 s |             0.79 / 0.83 s |                    1.11 / 0.76 s |

For 8,192 declarations, baseline peak RSS was 378,488 / 378,336 KiB and
candidate peak RSS was 395,908 / 380,728 KiB across the two runs. The extra
immutable index has a memory cost; these samples do not isolate its exact size.
A baseline `perf record -F 99 -e cycles:u` capture has 200 samples and no lost
samples: `Atom::as_str` accounts for 23.30% self cost and
`program_binding_target` for 3.57%. The binary lacks frame pointers, so this
capture does not support inclusive-stack attribution.

The no-import [registration control](2026_11_15_callback_registration.md) took
0.99 / 0.91 seconds at 512 callbacks, 3.73 / 3.44 seconds at 1,024, and
17.41 / 16.44 seconds at 2,048 (baseline / candidate). Those controls do not
exercise this lookup and do not establish a registration speedup. Every control
completed projection, analysis and apply. All three import cases, their repeats,
and all three registration controls have byte-identical saved analysis bodies
between binaries after excluding only the engine provenance header.

[The receipt](2026_11_15_import_binding_identity.json) records binary/input/body
identities and resource observations. Canonical Excalidraw was not rerun for
this narrow change; its remaining promotion bottleneck is unresolved.

## Residual promotion boundary

This lookup is separate from the repeated promotion cost identified in
[the callback profile](2026_11_15_availability_trace_storage.md#remaining-callback-promotion-work).
An already-promoted source owner does not establish an idempotent operation:

- `record_program_cell_read` appends ordered availability observations. Their
  multiplicity matters to exact schedule comparison, including assumed reads;
  ignoring assumed observations is reserved for authorized commutations.
- `taint_objects` can havoc captured lexical cells even without a class in the
  current promotion. Stable source identity alone cannot witness stable values.
- `promote_class_members` can invoke captured superclass functions and apply
  runtime rebind effects, changing cells and retained graph edges.
- The current graph memo validates roots only after evaluating the source's
  current reads. It therefore cannot remove that evaluation or effect recording.

Reusing the large effect sets would need an explicit dependency witness and a
representation that replays ordered availability multiplicity without walking
every binding again. A source-owner cache that skips these obligations is not
implemented. The immutable name index does not establish a canonical throughput
improvement or eliminate the remaining callback cost.
