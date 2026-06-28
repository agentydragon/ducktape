# Large Bundle Selector CSP Profile

This note tracks the current profiler-backed state of the global selector
assignment path on a large private downstream bundle. It intentionally avoids
naming the downstream product or copying any private source/code excerpts.

Old per-run analysis has been pruned from this file. Use the latest profile
below as the active baseline.

## Current Measurement

The active profile is a direct replay of the downstream debundle action after
PR #2635 ("Model debundle selector arithmetic as linear constraints"). The run
used the pre-squash branch commit `72eeb249b933fefd1ae71c71e849c4029592bf63`,
whose changes are now on `devel` via PR #2635.

```sh
/usr/bin/time -v timeout --foreground --kill-after=30s 120s \
  env DUCKTAPE_DEBUNDLE_PROGRESS=1 \
  perf record -F 99 -e cycles:u --call-graph dwarf,8192 \
  -o <profile-dir>/perf.data -- <direct-replay-wrapper>
```

Result:

- exit status `124`;
- elapsed wall time `2:01.20`;
- user CPU `120.63s`;
- sys CPU `5.54s`;
- max RSS `1,106,036 KB`;
- profile window approximately `119.7s`;
- `12,582` user-space cycle samples;
- no lost samples in the completed flat `perf report`;
- no CP-SAT request proto and no CP-SAT summary JSON were emitted.

The run did not reach OR-Tools. It timed out inside Rust-side work before the
sidecar request existed.

The debundle progress stream reached:

```text
[debundle progress] chunk=... phase=materialize_logical_chunk state=start
[debundle progress] chunk=... phase=compute_chunk_analysis state=start
[debundle progress] chunk=... phase=compute_chunk_analysis state=end
[debundle progress] chunk=... phase=resolve_global_selector_members state=start
```

There was no `resolve_global_selector_members state=end`.

## Profile Evidence

The completed flat report is dominated by Rust collection and iterator work. In
the large `cpu_core/cycles/u` bucket, top self-cost symbols include:

- `alloc::collections::btree::mem::replace` at `11.46%`;
- `Iterator::any::check::{{closure}}` at `8.57%`;
- `core::slice::raw::from_raw_parts::precondition_check` at `4.95%`;
- `__memmove_avx_unaligned_erms` at `4.78%`;
- `malloc`, `_int_malloc`, `_int_free`, and `malloc_consolidate`;
- BTree navigation/search frames.

Representative time slices show two distinct high-level costs:

- around the middle of the run, samples are in `analysis::purity` and SWC AST
  visitors such as `PlainDataWriteScanner` and
  `is_ts_enum_iife_call_for_binding`, so chunk analysis is still materially
  expensive;
- near the end of the run, samples are inside
  `selector_constraint_model_builder::compile_selector_problem` via
  `FactDomains::from_program_and_facts`, especially `add_facts`,
  `add_fact_strings`, `add_node`, `add_string`, `add_program_constants`, and
  `add_atom_constants`.

A late representative slice had all sampled selector-model stacks passing
through:

```text
compile_selector_problem
  FactDomains::from_program_and_facts
    FactDomains::add_facts / add_fact_strings / add_program_constants
      BTreeSet::insert / BTreeMap::insert
```

That is the current first bottleneck. The run is still building global fact
domains and derived indexes. It has not yet reached allowed-table lowering,
protobuf request emission, or CP-SAT solving.

## Current Root Cause

After #2635, arithmetic constraints are no longer being materialized as large
tuple relations. The remaining front-of-pipeline problem is broader:

```text
all facts -> global BTreeSet/BTreeMap domains and derived relations -> variables
```

`FactDomains::from_program_and_facts` eagerly scans every selector fact, clones
many strings, inserts owners/nodes/strings/ordinals into ordered sets, builds
derived relations, then walks all selector atoms to add constants. On a large
bundle, this makes the path spend significant time and memory before the CSP
solver can see any model.

This is not just an implementation detail. It keeps the Rust compiler in a
database-like relation materialization phase even though the production goal is
to describe the assignment problem compactly and let the solver propagate.

The likely algorithmic mistakes are:

1. **Eager global domains.** Every value of each type is gathered before the
   compiler has narrowed variable domains from the atoms that mention them.
2. **Ordered heap-heavy storage.** `BTreeSet<String>` and `BTreeSet<(..., String)>`
   make insertion and comparison expensive; determinism should come from final
   sorting/deduping compact ids, not from ordered insertion of cloned strings.
3. **Universal derived facts.** Derived relations are built whether or not the
   current selector program needs that relation.
4. **Late interning.** The builder still spends real time on typed facts and
   strings before compact backend ids become the primary representation.
5. **Remaining tuple-style lowering.** Child-list patterns and relation atoms
   still have paths that enumerate supports into allowed tables. This is not
   the end-of-run hot stack in the current profile, but it is the next model
   shape to revisit after domain construction stops dominating.

## Next Actions

1. **Add a pre-solver model-build summary that is emitted before backend
   request creation.** Timeout runs must report selector program counts, fact
   relation cardinalities, global domain sizes, per-variable candidate sizes,
   derived relation counts, and allowed-table histograms if reached. Keep this
   as data output, not manual timing instrumentation.
2. **Replace eager `FactDomains` BTree construction with compact dictionaries.**
   Intern strings and typed ids once, collect into vectors/hash tables during
   fact ingestion, then sort/dedup at the boundary where deterministic output is
   needed.
3. **Build only demanded indexes and derived relations.** Inspect the
   `SelectorProgram` first, identify which atom kinds and constants are present,
   and construct only the relation views needed by those atoms.
4. **Derive candidate domains before materializing variables.** For each atom,
   compute support sets after constants are applied; intersect supports per
   variable; fall back to full domains only for genuinely unconstrained
   variables.
5. **Keep relation rows compact from the start.** Lower facts and allowed rows
   to interned integer ids before tuple construction; avoid cloned
   `ConstraintValue`/`String` rows on the production path.
6. **Revisit child-list and relation atom lowering after domain construction is
   no longer the measured blocker.** Prefer solver-level constraints or compact
   table/element encodings over enumerating large intermediate relations in
   Rust.
7. **Only tune OR-Tools after a CP-SAT request exists.** When the run reaches
   the sidecar, profile the saved proto through the C++ solver separately and
   use CP-SAT stats to decide whether model changes, backend parameters, or
   search strategies are justified.

## Immediate Plan

The next PR should target the high-level Rust-side shape, not solver heuristics:

1. add the pre-solver summary so future two-minute capped profiles are
   informative even when the sidecar is not reached;
2. make `FactDomains` demand-driven and compact-id based;
3. reprofile the same downstream direct replay under `perf` with the `120s`
   cap;
4. if the run reaches the sidecar, capture CP-SAT problem-size stats and profile
   the sidecar separately.

For future timeout investigations, prefer stopping instead of immediately
killing the profiled debundler at the cap. A stopped process can be inspected
with `gdb` to read live stacks, heap shape, and relation/model state without
writing a large core file. Dump core only if the exact interrupted state must be
kept after the process exits.

The pre-solver summary side files should be enabled during these runs. They
report selector-program counts, input fact relation cardinalities, model-build
domain/relation counts when reached, and compiled CSP shape when reached:
variables by domain, domain-size histograms, allowed-table row/cell histograms,
linear/binary/all-different counts, and broad `constraint_count_by_kind`.
