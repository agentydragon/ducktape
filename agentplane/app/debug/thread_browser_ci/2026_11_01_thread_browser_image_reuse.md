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

The original optimization changed only this test's `test.runner-recycling-key`. Postgres servers,
databases, Electric state, Kubernetes API server, application services, ingress,
and browser contexts retain their existing fresh fixture lifetimes.

The November 2 extension below applies the same image family to
`//agentplane/app:test_thread_window_browser`.

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
The retained implementation uses the simpler local recycling key; remote sharing
remains a measured alternative for a later decision.

## Adoption

`test_thread_browser` and `test_thread_window_browser` share one execution-property
constant in `agentplane/app/BUILD.bazel`. It uses `thread-browser-images-v1-` followed by the four full
infrastructure image digests, in a fixed order. `postgres_18`, `electric_1_8_1`,
`ryuk`, and `nginx_unprivileged` are owned by `devinfra/image_pins.json`; its module
extension both creates their OCI repositories and exports the digest constants
loaded by these targets. Repinning any of these images automatically selects a new
snapshot baseline, without a second hash declaration or manual version bump.

The first full run prepares any cold placements; there is no seed job. The dated
keys above identify the measured experiment, not the retained configuration.
The existing `util.oci.load_oci_image` checks the runfiles layout digest and actual
daemon image ID before skipping a load. A missing or changed image therefore
loads normally, including on a cold executor. Frequently changing frontend
images use this same guard without changing the infrastructure key.

The [pin-derived configuration validation](https://app.buildbuddy.io/invocation/fba4444e-5a21-4b37-b6ec-cd468a6e9007)
passed all 42 cases on 16 originating shard actions with test-result caching
disabled. The case-identity hash matched the experiment. An inspected REAPI
Action carried exactly the four catalog digests in its recycling key. All four
moved pin records retained their image repository, digest, tag and platform.

## Window-browser extension (2026-11-02)

Execution window: 2026-11-02 09:26–09:40 UTC.
Base: `7244d8cb1cf92dde4e8dbbd52b7a9151f0a00242`. The extension preserves the
existing browser recycling key and shares its expression between the two targets.
Both register `agentplane.app.testing.thread_browser`; their existing fixtures
load Ryuk, PostgreSQL, Electric and nginx even when either target runs first.
The built frontend remains outside the stable key and uses the existing
layout-digest/daemon-image-ID check. No fixture lifetime or test behavior changes.

Runs used the command above with `//agentplane/app:test_thread_window_browser`
in place of the original label. Build/action caches remained enabled and
test-result caching was disabled. All 30 case executions passed without failures
or skips. Each invocation executed all six cases in one originating physical
TestRunner action. Their sorted case-identity SHA-256 was
`4d81dcc7ddd16faed7db5cfa83c92525b64934254ba30cdab5029a7de8ceb6c6`,
using the same newline-delimited format as the original experiment.

| Configuration       | Bazel elapsed | Process |     CPU | Imports | Import time | Invocation                                                                            |
| ------------------- | ------------: | ------: | ------: | ------: | ----------: | ------------------------------------------------------------------------------------- |
| Default control A1  |       146.0 s | 139.3 s | 303.5 s |       5 |      26.9 s | [c2bee346](https://app.buildbuddy.io/invocation/c2bee346-bd7d-4674-afb8-e881f041b67a) |
| Shared key, cold B1 |       137.5 s | 130.1 s | 288.4 s |       5 |      23.9 s | [a842bfff](https://app.buildbuddy.io/invocation/a842bfff-1f5b-4c2a-946a-46e310613702) |
| Shared key, warm B2 |       104.3 s |  97.3 s | 181.6 s |       0 |       0.0 s | [041da896](https://app.buildbuddy.io/invocation/041da896-bbb8-4186-91fd-503f23227a68) |
| Default control A2  |       156.2 s | 150.3 s | 329.6 s |       5 |      30.1 s | [6b507c16](https://app.buildbuddy.io/invocation/6b507c16-065a-4b5b-888b-d33960e1cf0b) |
| Shared key, warm B3 |       104.4 s |  97.7 s | 182.5 s |       0 |       0.0 s | [e0d33533](https://app.buildbuddy.io/invocation/e0d33533-0164-4342-8b43-b1dd4b53e435) |

The two-control mean was 151.1 seconds versus 104.4 seconds for the two warm
runs: 46.7 seconds / 30.9% less target-invocation time. Mean process time fell
144.8 → 97.5 seconds and CPU time 316.6 → 182.1 CPU-seconds. Avoided image imports
averaged 28.5 seconds inside the process time; do not add that to the elapsed
saving. The residual difference is not separately attributed. These are two
controls and two warm repeats, not fleet percentiles or whole-CI savings. The
first keyed run was cold and showed no reuse benefit; cold fallback passed.

The control and keyed REAPI input root was identical:
`81d92285789a1f14409ea949dbf575486191b1158e4a7dae783a7c2dba4b05ed/165`.
Arguments and environment matched; the only Command change was the recycling
platform property. The first keyed run and both warm repeats had identical Actions.

The first keyed run saved a new local snapshot; both warm repeats'
`lastExecutedTask` identify that first run on the same executor. Both skipped all
five image imports. All five runs recorded fresh history-Postgres initialization.
All 11 PNG artifacts from B2 matched the first control byte for byte; the retained-reader
and offline-recovery captures were visually inspected.

## Scope and follow-up

- This change applies to `test_thread_browser` and `test_thread_window_browser`;
  other PostgreSQL tests are unchanged. The window target's test result-cache key changes;
  compiler/linter execution platforms and the global RBE image stay unchanged.
- Both browser targets use the same `thread_browser` fixture plugin and load
  the family's required stable images, so either target can seed a cold baseline.
  Any further extension must ensure every possible first action loads them.
  Preserve the existing digest/daemon-ID guard and cold fallback, and derive
  the family's recycling key from its authoritative infrastructure image pins.
- Frequently changing frontend images should continue through the existing
  digest guard rather than forcing all stable infrastructure images cold on
  every source commit.
- Measure hit rates across normal CI heads, host turnover, time gaps and image
  changes before translating the observed warm gain into a fleet expectation.
  Local warm reruns exercised the same shard placements; this is not a soak.
- Whole-CI feedback savings depend on the remaining required critical path.
  Earlier full-system samples ended on VM tests minutes after browser tests;
  the standalone target reduction cannot be applied to those runs wholesale.
