# Callback registration: a small quadratic analysis reproduction

The bundle creates independent closures through one factory and attaches each
closure's captured reader as an own data property:

```js
function make(read) {
  const callback = () => read();
  Object.defineProperty(callback, "read", { value: read });
  return callback;
}
const callback0 = make(() => 0);
// Repeat with independent readers up to callback1023.
const result0 = callback0();
console.log(result0, callback1023.read());
```

The spec keeps the entire chunk in its residual entry. No packages, imports,
classes, constructors, host hooks, extraction assignments, or annotations are
needed. The lexical control omits `Object.defineProperty` and calls the final
closure directly. Both print `0 1023`.

## Measurements

Engine code: `46648793df6d00e2bda9d58f633929a3d9df13ca`; corpus and e2e tests:
`89a4f190454a88dd7c339fdba085e2513ec53a46`. Optimized binaries with frame
pointers, built by [this invocation](https://app.buildbuddy.io/invocation/9c5133d1-2e79-442b-af6f-ad186a7a92c6).
Each stage ran as a separate native process on the same Linux host with
`RAYON_NUM_THREADS=4`. Analysis ran under `perf record -m 8 -F 99 -e cycles:u
--call-graph fp`; `/usr/bin/time` measured each process. These are single runs
on a shared machine, including profiler overhead, not a latency distribution.

| Case                   | Source bytes | Projection wall / user CPU | Analysis wall / user CPU | Apply wall / user CPU |
| ---------------------- | -----------: | -------------------------: | -----------------------: | --------------------: |
| 512 attached readers   |       18,931 |              0.04 / 0.01 s |            0.80 / 0.69 s |         0.09 / 0.05 s |
| 1,024 attached readers |       37,924 |              0.05 / 0.02 s |            3.23 / 2.63 s |         1.51 / 0.08 s |
| 2,048 attached readers |       77,860 |              0.04 / 0.02 s |          12.77 / 12.17 s |         0.18 / 0.14 s |
| 1,024 lexical readers  |       37,859 |              0.03 / 0.02 s |            0.28 / 0.14 s |         0.09 / 0.07 s |

Doubling registrations increases analysis time roughly fourfold. The lexical
control is about 12 times faster in wall time at 1,024 registrations. The apply
wall-time outlier has little CPU cost; it does not explain the analysis scaling.
Every case passed the realizability and translation validators.

The 2,048-reader profile contains 1,278 samples and no reported lost samples.
Weighted inclusive cycle shares: `bounded_invocation_fallback_transfer` 55.3%,
`walk_callback_values` 31.9%, `invoke_with_cells` 61.7%. Inclusive shares overlap.
The small input reproduces repeated conservative callback work; this alone does
not establish how much of any larger input's runtime it explains.

## Why the native transfer is refused

`explain-integrity --id callback0` reports `unknown_call_target` and
`opaque_execution` with admission refusal `unknown_target` at the
`Object.defineProperty` call. `native_call_identity` was evaluated; there are no
failed integrity guards or slot witnesses in this query.

`resolve_native_call_transfer` in [interpreter.rs](../facts/sync_effects/interpreter.rs)
routes `Mutation::DefineProperty` through
[`descriptor_heap::dynamic_key_definition`](../facts/sync_effects/world/descriptor_heap.rs).
Its target must be `Value::Alloc(Kind::Object, ...)`, with additional proofs
about a current-statement literal, settled properties, writability, and ordinary
descriptor conversion. A returned function does not satisfy that object-target
check. The conservative execution path then repeatedly walks reachable callback
values as registrations accumulate.

This is a missing native transfer capability, not evidence that JavaScript
cannot attach the property. A sound extension needs function-instance identity
and property/descriptor rules; broadening the object-kind match alone is not a
correct implementation.

## Reuse observation on this exact input

The 1,024-reader input also ran on engine
`0b858886e6ff9026bbb01fb7fbec512433afecde`: analysis 2.84 s wall / 2.52 s user
CPU, with the same accepted outcome. An observation-only
[probe branch](https://github.com/agentydragon/ducktape/tree/experiment/callback-reuse-census-current-20261114)
at `5a7e7970133392c1cb1e8891c01642cdc97182a9` recorded dependencies while still
performing every production walk. The probe and its uninstrumented base produced
byte-identical saved-analysis bodies after removing their engine provenance
headers. This probe is experimental instrumentation, not a shipped cache.

- 9,241 walks visited 17,969,293 nodes and 14,781,538 edges.
- Repeated whole queries with unchanged dependencies account for 57,436 visited
  nodes: only 0.32%. Root sets mostly grow rather than repeat exactly.
- Of 9,530,461 individual expansion events, 9,522,260 had unchanged previously
  observed inputs: 99.91%. There were 5,121 changed expansions.
- All five callback-pool operations completed one pass and fell back at pool
  size 2,050. The quadratic cost does not require a multi-pass fixpoint.
- The bounded whole-query observer cleared 12 times; the node observer never
  cleared. These are observations within those windows, not an unbounded reuse
  proof.

Exact whole-query memoization would miss nearly all node visits here. The next
algorithmic experiment should examine incremental reachability or dependency
validated reuse of per-value expansions, while preserving traversal semantics
when any dependency changes. Unchanged direct adjacency alone does not prove an
unchanged transitive closure or permit reusing callback execution effects.
Separately, modelling the native operation could prevent this particular opaque
call from seeding the work. Neither optimization is implemented by this change.

## Reproduce

Use the repository devshell. Choose a fresh task scratch directory. Generate a
standalone bundle and spec through Bazel:

```sh
scratch="${TMPDIR:-/tmp}/agents/$(date +%Y_%m_%d)/callback-registration"
mkdir -p "$scratch"
bb run //devinfra/js/debundle/perf:gen_callback_registration_corpus_bin -- \
  --out "$scratch/attached-1024" --callbacks 1024 --invocations 1
```

The generated `spec.json` includes absolute input and output paths and no analysis
annotations. Repeat with `--callbacks 512` or `2048` and a different `--out`.
Add `--omit-attached-method` for the lexical control.

Build the optimized native tools:

```sh
bbr build -c opt \
  --@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes \
  --remote_build_event_upload=all \
  //devinfra/js/debundle:debundle_project_analysis \
  //devinfra/js/debundle:debundle_analyze //devinfra/js/debundle:debundle
```

Download the three declared executables from that invocation and verify their
advertised digests, as described in
[Bazel integration](../docs/bazel_integration.md#preserve-the-pipeline-cli-from-rbe).
Set `projector`, `analyzer`, and `debundler` to those executable paths. With the
generated output directory still empty:

```sh
corpus="$scratch/attached-1024"
"$projector" --spec "$corpus/spec.json" \
  --out "$corpus/analysis-projection" --apply-out "$corpus/apply-projection" \
  --projection-root "$corpus"
RAYON_NUM_THREADS=4 perf record -m 8 -F 99 -e cycles:u --call-graph fp \
  -o "$corpus/perf.data" -- \
  "$analyzer" --projection "$corpus/analysis-projection" \
  --tree-source-root "$corpus" --out "$corpus/analysis"
"$debundler" run --spec "$corpus/spec.json" --analysis "$corpus/analysis" \
  --apply-projection "$corpus/apply-projection" --projection-root "$corpus"
```

For graph-backed explanations, reuse the analysis with `run --spec ...
--analysis ... --graphs --reports-only <fresh-directory>`; `--graphs` cannot be
combined with `--apply-projection`. Query that report tree's
`dependency_graph.json.gz` with `explain-integrity --graph ... --id callback0`.

The behavioral check is:

```sh
bbr test //devinfra/js/debundle/e2e:callback_registration_scale_test
```

It passed both cases in the linked invocation (7.6 s for the target). Each test
uses the real CLI and a residual-only spec, then executes the original and
emitted JavaScript and checks `0 1023\n`. There is no timing threshold: the test
pins valid emitted behavior for future performance changes. Measurements,
source hashes, binary identities, and probe counts are recorded in the sibling
[JSON receipt](2026_11_15_callback_registration.json).
