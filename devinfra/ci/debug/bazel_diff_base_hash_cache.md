# Cross-run bazel-diff base-hash caching

Status: deferred. Do not recommend this again from the base command's duration
alone. Explain what changed in the graph, toolchain, runner reuse or cache
transport, then measure the marginal saving across the complete CI driver.

## Why the base command is not the saving

`bazel_ci.py` queries the base and candidate in the same Bazel server, then runs
test and build without a shutdown. The base query loads shared graph/Skyframe
state. A cached base JSON can remove one hash command while moving that loading
work to the candidate query. It does not itself supply a Bazel analysis cache.
Whole-driver measurements must include any subsequent test/build analysis.

The [5 October report](https://github.com/agentydragon/ducktape/blob/783d6b76b7173a10fb63b2392be7149599676c74/index.html)
already deferred this proposal: the **entire warm selection sequence** took
16.461/16.456 s, and ten diagnostic jobs used eight distinct bases. That was a
small sample, not a cache-hit-rate estimate; no cross-run hash cache was actually
implemented or benchmarked. The same investigation measured a separate 27.054 s
warm-driver improvement from retaining the Bazel server between selection and
test ([#9069](https://github.com/agentydragon/ducktape/pull/9069)). Preserve that reuse.

The 30 October report incorrectly revived a 5–15 s cache saving without a matched
intervention. That estimate is withdrawn. The older July transport sketch also
predates these measurements; its per-command timings do not establish savings.

## 30 October matched experiment

[BuildBuddy runner and complete experiment script](https://app.buildbuddy.io/invocation/69c90c0d-da56-4a01-a47e-c60b7e229d57).
Input: synthetic merge `21d508c9905015199bcfd82d27a8fa9f326ea8a6`, base
`a854463d14d9527cddee21b8d1ea06f39376de0f`, PR head
`d3770d3ad6f5dbc8e7138fae405b1edd5f26e8ad` (debundle test CLI stripping).

The experiment profiles the unmodified driver's `main()` with Python `cProfile`.
The treatment replaces only the base checkout and base hash generation with a
local copy of a precomputed base JSON, verified by digest. Candidate hashing,
Git fetch/merge validation, target queries, test/build, lint aspects, Go selection
and diagnostics remain active. Every observation must select identical labels
and the identical test query; RBE and remote caches remain enabled.

Two reversed-order pairs restart the Bazel JVM before each observation. Eight
subsequent observations use balanced order (normal, hit, hit, normal, hit, normal,
normal, hit) with the JVM retained.
"Cold" means a fresh JVM with disk, repositories and remote caches retained, not
a fresh machine. The producer runs separately; the local hit excludes production,
storage, lookup and transfer cost, so it is an optimistic consumer comparison.

| Observation                                             | Normal driver, s | Local-hit driver, s |
| ------------------------------------------------------- | ---------------: | ------------------: |
| First fresh JVM (repository/toolchain warm-up confound) |           94.628 |              59.533 |
| Later fresh JVM pair, reversed order                    |           64.381 |              60.536 |
| Retained JVM (warm-1, warm-2)                           |           18.057 |              12.449 |
| Retained JVM (warm-4, warm-3)                           |           18.267 |              12.431 |
| Retained JVM (warm-6, warm-5)                           |           17.040 |              12.007 |
| Retained JVM (warm-7, warm-8)                           |           17.175 |              11.830 |
| Retained JVM median (four each)                         |           17.616 |              12.219 |

The later fresh-JVM pair saves **3.845 s**; the warm medians differ by
**5.397 s**. All 12 runs succeeded and selected the same 176 targets and test
query. This confirms a small consumer saving on this input, not a fleet feedback
estimate or a reason to revive the old 30–60 s claim.

The first normal run fetched Rust tooling: its Bazel profile includes a 19.669 s
Rust toolchain repository event and 41.164 s test analysis/execution, versus
8.756 s for the later normal run. Its larger apparent saving is confounded by
first-use repository/toolchain work. The separate initial producer took
69.447 s, including cold setup; that is not an estimate
of a warm producer's cost. No storage/lookup transport or production hit rate was
measured. Keep caching deferred until those costs and reuse justify its complexity.

The runner log contains all rounds, the experiment script and the uploaded
profile archive digest:
`/compressed-blobs/zstd/f1c43a35f63c0e63792adc8fe7d3717456ab219da02a0cc1a6abdcbee9c2b393/109366`
(retrieve with `bb download`). The archive contains `results.json` and 13
`cProfile` files, including the producer. The tested driver matches `393cbd243da2db6070499dac2142e075eb231d09`
(`bazel_ci.py` SHA256 `dfae35cdf173deabd56207cdbb40f99ad0a37fe4f0d087a436ea9eefd247fa3a`),
with Bazel 9.2.0 and the source-pinned runner/tool configuration.

## Conditions for revisiting

- Show a changed mechanism: substantially larger repeated graph loading/hashing,
  a different runner/server lifecycle, or an available cache transport with lower
  production and lookup cost. A newer report date alone is not a change.
- Repeat matched full-driver profiles on fixed base/candidate inputs, with
  balanced order and representative warm/cold states. Compare target selections
  and retain tests, lint aspects, RBE and the warm-server behavior.
- Measure reuse frequency and cache availability across actual PR bases. Include
  producer cost, misses, lookup/transfer overhead and the next required gate when
  estimating compute or feedback savings.
- Define and validate the complete cache input key and trust boundary: source
  SHA, Bazel/bazel-diff versions, repository resolution and relevant configuration.
  On any mismatch or unavailable artifact, regenerate the base safely.
