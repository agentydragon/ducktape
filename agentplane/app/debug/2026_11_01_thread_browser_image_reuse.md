# Thread-browser image reuse on BuildBuddy RBE

## Result

A dedicated TestRunner recycling key eliminated repeated image imports in warmed
`//agentplane/app:test_thread_browser` runs. Two default-platform controls took
134.2 and 170.2 seconds; two warmed keyed runs took 82.5 and 93.8 seconds. All
runs executed and passed the same 42 cases across 16 shards.

The paired reductions were 38.5% and 44.9%. Comparing the two-control mean with
the two-warm-run mean gives 152.2 → 88.2 seconds: 64.0 seconds / 42.1% less
target-invocation time. Aggregate worker process time fell 29.8%, about eight
worker-minutes per run. These are small-sample, same-input experiment results,
not fleet percentiles or whole-CI feedback savings.

The spike changes only this test's `test.runner-recycling-key`. Postgres servers,
databases, Electric state, Kubernetes API server, application services, ingress,
and browser contexts retain their existing fresh fixture lifetimes.

## Method and evidence

Inspected base: `067f50b31262fa19c5d33228ebaec801bcf39c2b`.
Collection/execution window: 2026-11-02 04:39–05:03 UTC. The isolated checkout's
only runtime-affecting change was this execution property:

```starlark
exec_properties = {
    "test.runner-recycling-key": "thread-browser-images-20261101-v1",
}
```

Every full run used:

```bash
bbr test //agentplane/app:test_thread_browser \
  --nocache_test_results --test_output=errors \
  --test_arg=--durations=0 --test_arg=--log-cli-level=INFO \
  --test_arg=--log-disable=httpx --test_arg=--log-disable=httpcore
```

Build/action caches remained enabled. Test-result caching was disabled. For each
invocation, collected `bbapi execution`, artifact listings, all 16 JUnit and test
logs, fixture setup logs, the Bazel profile/critical path, and each physical
TestRunner's raw `bb execution get` response. All full-run TestRunner records
were originating executions (`invocationLinkType=1`), not reused results.

The sorted 42-case identity list has SHA-256
`0e6b544446d56f81c2c049c971aa8052813240c8b8a30acefaa0340787132dc8`
(one `classname::name` per line, trailing newline). It matched every full run;
there were no failed or skipped cases. Across seven full runs and two selected
one-case seeds, all 296 case executions passed.

For shard 4, retrieved the baseline and warmed variant's REAPI Action and Command.
The input root was byte-for-byte identical:
`da79db348fdb5de3b0d0d412737ec19d0712623846a4883e4084305a8d740226/165`.
The only Command difference was the added recycling platform property; arguments
and environment were unchanged. The Action differences were its Command digest
and platform. No profiler or new timing code was added: phase times come from
the existing OCI loader logs; process/CPU times come from execution metadata.

## Full-run observations

Runs are listed in execution order. Each row is 42 passing cases / 16 fresh
physical test actions. "Warm images" means a shard emitted no image-import
commands, not merely that its VM resumed from some snapshot.

| Configuration                       | Bazel elapsed | Longest action | Worker process sum |   CPU sum | Warm images | Import sum | Invocation                                                                            |
| ----------------------------------- | ------------: | -------------: | -----------------: | --------: | ----------: | ---------: | ------------------------------------------------------------------------------------- |
| Default control A1                  |       134.2 s |        124.8 s |          1,467.5 s | 2,994.1 s |        0/16 |    395.1 s | [72382961](https://app.buildbuddy.io/invocation/72382961-7a33-4e3c-b554-4b983ad36134) |
| Key, first concurrent run B1        |       138.7 s |        132.8 s |          1,632.2 s | 3,363.0 s |        0/16 |    485.4 s | [f5483c73](https://app.buildbuddy.io/invocation/f5483c73-1ef6-47df-8528-878b754948e5) |
| Key, warm B2                        |        82.5 s |         79.1 s |          1,097.5 s | 1,678.1 s |       16/16 |        0 s | [c04f66ea](https://app.buildbuddy.io/invocation/c04f66ea-7d08-43a1-a17d-951aa158c633) |
| Remote sharing, first concurrent C1 |       148.6 s |        139.4 s |          1,358.2 s | 2,298.2 s |       12/16 |    136.9 s | [e2977713](https://app.buildbuddy.io/invocation/e2977713-67b9-4b20-bb0b-eb0fba57999d) |
| Default control A2                  |       170.2 s |        162.2 s |          1,724.5 s | 3,336.6 s |        0/16 |    511.3 s | [96c20503](https://app.buildbuddy.io/invocation/96c20503-91ea-4e39-a2e4-0bd611a14ba2) |
| Key, warm B3                        |        93.8 s |         86.1 s |          1,142.5 s | 1,807.4 s |       16/16 |        0 s | [ef5497e5](https://app.buildbuddy.io/invocation/ef5497e5-1e76-4da3-a05f-cd128103289e) |
| Remote sharing, warm C2             |        98.2 s |         93.9 s |          1,203.1 s | 1,891.1 s |       16/16 |        0 s | [e9588c67](https://app.buildbuddy.io/invocation/e9588c67-d98e-4f91-af8c-d5d5fa264899) |

The controls each imported five images in every shard: Ryuk, PostgreSQL 18,
Electric 1.8.1, nginx ingress and the built frontend. The two warmed local-key
runs eliminated all 80 import commands. Their mean avoided import sum was
453.2 worker-seconds, versus 476.0 fewer worker process-seconds overall. Do not
add those quantities: the import work is already inside the process time.

Mean CPU time also fell 44.9% (3,165.3 → 1,742.7 CPU-seconds). CPU, summed
worker process time, longest action, and Bazel elapsed are distinct clocks.
No GitHub workflow or required-gate feedback distribution was measured.

## What the snapshots show

The default controls resumed existing VM snapshots, but their saved Docker state
did not contain the needed images. In the preceding investigation, three
ordinary-CI browser shards with 18–23 second PostgreSQL imports traced their saved
baselines to unrelated TypeScript validation, Ruff and copy actions on October 30.
BuildBuddy's ordinary-RBE snapshot path can repeatedly restore that first saved
point without saving later image imports back into it.

A [selected browser seed](https://app.buildbuddy.io/invocation/1cb4f536-0a5e-44f2-9a14-3486a5905821)
ran the archived-thread/reload case unsharded under the new key. It passed and
loaded all five images (24.5 s total imports; 81.2 s Bazel elapsed). It prepared
only one executor/configuration. The first full concurrent run landed on sixteen
other executors, so all sixteen actions were cold. The next full run restored
snapshots saved by those B1 actions; all sixteen skipped imports and matched B1's
physical executor hostnames shard-for-shard. Thus a separate one-case prewarming
job is not necessary for the local-key version: the first normal full run warms
the shard placements, and the following run demonstrated the gain.

Warm-image actions still logged fresh PostgreSQL startup. The ordinary and warm
cohorts each had sixteen history-Postgres `initdb` completions, confirming fresh
database servers rather than retaining live PostgreSQL. Fixtures and per-case
database cloning code were unchanged.

Downloaded and visually inspected the phone chronological-debug and desktop
reading-anchor screenshots from A1/B2. Both corresponding PNG pairs were
byte-identical. This is a selected visual check, alongside the unchanged full
behavioral suite, not a pixel comparison of every screenshot.

## Remote-sharing sub-experiment

Tested a separate family with:

```starlark
exec_properties = {
    "test.allow-remote-snapshots": "true",
    "test.runner-recycling-key": "thread-browser-images-remote-20261101-v1",
}
```

Its [one-case seed](https://app.buildbuddy.io/invocation/3ae4dc87-8b89-4f4e-8034-32583f86cbdd)
passed in 74.1 s and reported both `savedLocalSnapshot` and
`savedRemoteSnapshot`. On C1, twelve actions on other executors restored that
seed, with zero imports. Four actions had a different VM configuration hash and
booted cold; they became the long tail. C2 restored prepared snapshots for both
configuration families and all sixteen actions skipped imports.

Remote sharing therefore works across hosts, but the first prepared configuration
does not cover every host CPU/VM configuration. The 98.2 s C2 observation does
not establish that remote sharing is intrinsically faster or slower than local
reuse: the host populations differ and there is only one fully warm remote run.
The retained spike uses the simpler local recycling key; remote sharing remains
a measured alternative for a production decision.

## Scope and follow-up

- This is an experiment on `test_thread_browser`; the window-browser companion
  and other PostgreSQL tests are not changed. Its test result-cache key changes;
  compiler/linter execution platforms and the global RBE image stay unchanged.
- Use a shared image-family key for compatible browser targets when expanding.
  Every possible first action must load the family's required stable images.
  Advance the family version with infrastructure image-pin changes; preserve
  the existing digest/daemon-ID guard and cold fallback. A production pin/version
  integration is not implemented by this dated experiment key.
- Frequently changing frontend images should continue through the existing
  digest guard rather than forcing all stable infrastructure images cold on
  every source commit.
- Measure hit rates across normal CI heads, host turnover, time gaps and image
  changes before translating the observed warm gain into a fleet expectation.
  Local warm reruns exercised the same shard placements; this is not a soak.
- Whole-CI feedback savings depend on the remaining required critical path.
  Earlier full-system samples ended on VM tests minutes after browser tests;
  the standalone target reduction cannot be applied to those runs wholesale.
