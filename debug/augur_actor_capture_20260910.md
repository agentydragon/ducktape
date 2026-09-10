# Actor capture and JSON-transfer evidence

Measured source: `8325c94bfbfc903a542f74a7a492521f8db7b074`. These measurements
precede the holding-pool declaration rebase and the Python-owned action session.
They measure the native batch actor consumer at that source, not the future
session boundary or an executor-language comparison. Reproduce the workload with
<../finance/augur/x/monthly_actions/README.md#population-capture-and-profiling>.

## Matched workload

Sixty requested months, alternating fixed $100/$50 share prices, two shares with
$40/unit basis, one $150 bill and the synthetic 10% long-term-gain tax schedule.
The authored rule liquidates to pay the bill. Odd original path IDs stop in event
month 0; even IDs pay the $12 tax claim in month 12 and finish with $38. Later
decisions are empty; later annual assessments contain no new income or gains.
These are repeated deterministic controls, not independent probability samples
or a general retirement workload with recurring consumption/rebalancing.

Every capture mode executes the same actor/month path and returns the same
selected compact results. Summary retains numeric observed account/public-pool
series, payment occurrences, canonical tax records, the exact ending book and
final attempted prefix. Dense additionally retains event tables and historical
books; Forensic additionally retains the journal. Neither mode invents observations
after stopping. Thus 1,000 paths observe 30,500 path-months, not 60,000.

Each row is one fresh Linux RBE process with optimized Rust (`-c opt`), seven
reported logical CPUs and four Rayon threads. cProfile covers invocation through
decoded JSON: process startup, native input parsing/execution/capture, output
file writing/reading and Python JSON decoding. Input preparation/file creation
are outside the profile. No warm-repeat distribution, tail latency, pure native
compute time, Python-policy time or per-call session transfer was measured.

## Observed results

|   Paths | Capture  | Observed path-months | Profiled seconds | Output bytes | Self RSS KiB | Child RSS KiB |
| ------: | -------- | -------------------: | ---------------: | -----------: | -----------: | ------------: |
|   1,000 | Summary  |               30,500 |            0.152 |    5,315,404 |      256,788 |       232,484 |
|   1,000 | Dense    |               30,500 |            1.378 |   70,798,794 |      642,556 |       234,104 |
|   1,000 | Forensic |               30,500 |            1.289 |   72,018,794 |      649,008 |       232,172 |
| 100,000 | Summary  |            3,050,000 |           19.843 |  531,738,904 |    3,031,628 |     1,633,360 |

RSS is a separate process high-water mark including input preparation, sampled
before verification/hash construction. A child may inherit the parent image at
fork. These maxima are not additive simultaneous peaks or isolated Rust heap
measurements. The single Dense/Forensic timing ordering is not evidence that
more capture is faster.

At 1,000 paths, cProfile attributes 0.090 seconds to native subprocess wait and
0.054 seconds to JSON decoding for Summary. Dense spends 0.365 seconds waiting,
0.940 decoding and 0.068 reading output; Forensic spends 0.368 waiting, 0.848
decoding and 0.068 reading. At 100,000 paths, Summary spends 12.080 seconds waiting,
7.232 decoding and 0.467 reading. Wait includes native parsing, financial work,
capture and output writing; it is not pure evaluator time. Nested cumulative
profile rows must not be summed as independent costs.

Compact output substantially reduces retained/serialized data on this control,
but a 532 MB document and roughly 2.9 GiB Python process high-water mark at 100,000
paths are still material. This provides evidence for later typed/selected-result
transfer work, not an agreed cost budget or proof of an optimal representation.
No runtime/language or supported hybrid-boundary decision follows from these rows.

## Agreement and retained evidence

All 1,000-path runs have byte-identical prepared inputs and identical hashes of
original-ID/summary/stop results after removing the optional trace. The high-N run
changes population size; its different digest is expected.

|   Paths | Input bytes | Input SHA-256                                                      | Compact result SHA-256                                             |
| ------: | ----------: | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
|   1,000 |     398,902 | `806038321f5bb6feb5f37187b0bea5fad67f20b4be081f098c8e472520f6c145` | `8810ff8799508c27e030ecfe584222135083b5c0322f6d5d166b97f7a3b77fc1` |
| 100,000 |  39,652,404 | `d6b214048338e64cbd74e9a8fabd05ebbda293283fe9ceb6259c32ed1c50a369` | `a69cd59c5dffeb0c1416a7fb8884fdec47cfbd615d8c3633f21ee70a09d4c028` |

The runner artifacts retain `report.json`, `execution.prof`, `execution-input.json`
and `outcomes.json` under the named `command-0/` directory. Links below identify
the outer runner containing program output/artifacts, not merely the build child.

| Workload          | Runner                                                                                          | Artifact subdirectory   |
| ----------------- | ----------------------------------------------------------------------------------------------- | ----------------------- |
| Summary / 1,000   | [Report and profile](https://app.buildbuddy.io/invocation/458edd33-16fd-466b-880e-952af71a8728) | `p10-summary-1000x60`   |
| Dense / 1,000     | [Report and profile](https://app.buildbuddy.io/invocation/8f96af6a-ff9b-4b51-92d1-c75daa794dd3) | `p10-dense-1000x60`     |
| Forensic / 1,000  | [Report and profile](https://app.buildbuddy.io/invocation/6ad4991c-feae-41f3-a06e-6039e9b5f2e7) | `p10-forensic-1000x60`  |
| Summary / 100,000 | [Report and profile](https://app.buildbuddy.io/invocation/1b482baa-7a5d-4dc9-bd12-d076f9f485f1) | `p10-summary-100000x60` |

The actual population/profile CLI tests passed at measured source in
[focused verification](https://app.buildbuddy.io/invocation/99332a62-0289-4e0f-b0a2-5eedeed4b09f).
They check small generated populations, compact/forensic fingerprints, selected
replay and preservation of original path IDs. The native capture tests separately
exercise changing prices at failure, repeated claim labels with distinct handles,
successful-prefix preservation and unchanged next-policy observations.
