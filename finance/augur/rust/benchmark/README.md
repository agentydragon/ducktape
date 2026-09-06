# Augur simulator benchmark

The throughput driver and the measured baselines. The scenario it runs is not this
package's and lives in [../../benchmark/README.md](../../benchmark/README.md);
`fixture.py` here only writes that compiled plan out as the integer document, which the
standalone binary needs on disk and the in-process bindings do not.

Fixture generation and JSON parsing happen outside timed regions. The optimized target
also validates the fixture once before cold and warm timing.

These are measurements, not performance gates.

## 2026-08-26 feature-rich dense benchmark

500 rollouts × 60 transitions retaining dense monthly state and all canonical event
channels. Attempts at 2,000 dense rollouts exceeded the runner memory limit, so 500 is the
largest recorded dense run. Five warm repeats on a seven-logical-CPU BuildBuddy runner
class.

```text
bbr run -c opt //finance/augur/rust/benchmark:driver_bin -- \
  --output-mode dense --rollouts 500 --horizon-months 60 --repeats 5
```

- cold execution: **5.1057 s**;
- warm median: **0.4768 s**;
- warm runs: 0.4768, 0.5794, 0.4750, 1.7043, 0.4727 s;
- throughput: **1,049 rollouts/s**;
- throughput: **62,921 rollout-months/s**;
- peak child RSS: **3,061,728 KiB** (2.92 GiB);
- retained state snapshots: 30,500;
- retained native event records: 547,875;
- corresponding canonical event rows: 479,375;
- BuildBuddy invocation: `d6e2796d-b760-471a-9527-ed94c5eb0db6`.

The dense layout is not the intended production one: it stores rollout-major rich structs
and repeats static string metadata in snapshots and events. Factoring that metadata out
once and retaining numeric state and event columns is the remaining memory win, and this
benchmark is the one to rerun after it.

## 2026-08-24 compact population baseline

The compact path retains fixed-size final summaries per rollout and allocates no monthly
snapshots, journals, or event traces. 100,000 rollouts × 60 transitions, five warm runs,
same runner class.

```text
bbr run -c opt //finance/augur/rust/benchmark:driver_bin -- \
  --rollouts 100000 --horizon-months 60 --repeats 5
```

- median: **2.0530 s**;
- runs: 1.9930, 2.0530, 2.0604, 2.1628, 1.9760 s;
- throughput: **48,709 rollouts/s**;
- throughput: **2,922,559 rollout-months/s**;
- peak child RSS: **316,580 KiB**;
- counted journal entries: 12,500,000;
- counted dispositions: 200,000;
- failed rollouts: 0;
- checksum: `13286037983044011749`;
- BuildBuddy invocation: `27f6c83e-8449-400e-b6f8-29c81380578d`.

These numbers predate the fixture-schema additions for private equity and TLH, so they
measure a narrower workload than the dense run above. The same state-machine code records
full traces in `simulate(...)`; benchmark checksums only prevent dead-code elimination.
