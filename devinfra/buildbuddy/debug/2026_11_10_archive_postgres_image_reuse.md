# Archive PostgreSQL image reuse on RBE

The [archive timeout investigation](https://github.com/agentydragon/ducktape/pull/15537)
found repeated PostgreSQL image imports in four small test actions. On commit
`975f68a29e4139cd66e0010a480d70c100c599c1`, those targets use the existing
digest-keyed PostgreSQL/Ryuk recycling family. This changes only the executor
family: the tests still start fresh PostgreSQL containers and create their own
databases. Remote snapshot sharing and the save policy remain unset.

## Cold and restored actions

Ran the four targets twice with `bbr test --nocache_test_results`; action and
build caches remained enabled. Each row is a fresh physical TestRunner action.
The first [invocation](https://app.buildbuddy.io/invocation/7528fb07-7d5f-4a19-a14b-c4e98913c765)
landed on four cold workers; the [repeat](https://app.buildbuddy.io/invocation/ffbe0e3f-d558-4ef0-985d-eeeb96ebc6d1)
restored three of those VMs and landed the fourth on a different cold worker.
Times are test-action durations, not CI critical-path savings.

| Target                         |    First action | First PostgreSQL import |  Repeat action | Repeat PostgreSQL import |
| ------------------------------ | --------------: | ----------------------: | -------------: | -----------------------: |
| `test_claude_title_projection` |  37.5 s, passed |                   8.0 s | 34.3 s, passed |                     none |
| `test_codex_title_rename`      |  42.3 s, passed |                   8.4 s | 32.6 s, passed |                     none |
| `test_codex_title_retry`       |  43.6 s, passed |                  12.2 s | 32.8 s, passed |                     none |
| `test_initial_import`          | 60.2 s, timeout |                  31.9 s | 50.0 s, passed |                   21.0 s |

The first initial-import action reached PostgreSQL readiness 4.1 seconds after
its import completed, shortly before the deadline. Its timeout does not isolate
the work still underway after readiness. In the repeat, its different worker
also imported Ryuk and PostgreSQL and reached readiness in 2.7 seconds.

For the other three targets, the repeat's `VMMetadata.lastExecutedTask` names
the matching first-run execution; VM IDs, executor hosts and configuration
hashes match. Their repeat setup logs contain no Ryuk or PostgreSQL import and
show fresh PostgreSQL readiness in 3.2–3.4 seconds. The same four pytest cases
passed in those three targets in both runs; initial import passed its one case
in the repeat. The three paired duration differences include other variable
work, so they are not a controlled estimate of fleet-wide savings.

The decoded first-run Action carries `recycle-runner=true`, Firecracker
isolation, and the expected `postgres-images-v1-...` key, with no
`allow-remote-snapshots` property. The test actions' distinct execution IDs and
`doNotCache` metadata confirm fresh executions; resuming a VM alone would not
prove the image was present.

## Hosted CI and limits

The [PR CI attempt](https://github.com/agentydragon/ducktape/actions/runs/42917751198)
tested synthetic merge commit `47e21db0`. Its outer BuildBuddy stream closed
while the task was claimed, leaving the [Bazel invocation](https://app.buildbuddy.io/invocation/5dce3ad4-e653-5143-bcf2-1473c2b33e40)
disconnected. Three test results were reported as passing. The physical
`test_initial_import` execution independently reported `deadline exceeded`;
its cold-VM output tree contains a setup log ending after PostgreSQL import
started, with no completion line. Bazel did not publish its final test result
before the disconnect, so the execution timeout and the stream failure are
separate observations.

This experiment shows useful local image reuse on three matched workers, not a
guarantee that an arbitrary placement will be warm. It neither establishes a
fleet hit rate nor fixes the cold-start timeout. The long cold imports and
remaining startup/test work need their own diagnosis if cold actions are to
meet the existing deadline reliably.

## Expanded archive targets

[PR #15572](https://github.com/agentydragon/ducktape/pull/15572) extended the
same family to 15 more archive targets, at head `446e196a`. Its first
[CI attempt](https://app.buildbuddy.io/invocation/112bb2da-c650-5e52-bbb2-776284445523)
tested synthetic merge commit `34b52f52`: nine of those 15 targets passed and
six timed out at 60 seconds. All 15 TestRunner actions started on distinct cold
VMs. Four were on hosts named `gke-*`; all passed, with PostgreSQL imports
lasting 8.3–11.1 seconds. Eleven were on `sjc-*` hosts; six timed out. Ten
completed PostgreSQL import in 18.5–32.6 seconds, while one had not logged
completion by its deadline. Hostname groups are observations, not proof of
different pools or of the cause of the timing difference.

Five of the six timed-out actions completed image import. Some also reached
PostgreSQL readiness and started pytest cases: `test_codex_title_candidates`
completed six of nine cases before its deadline. A slow import therefore does
not establish what each test was doing at timeout.

The [retry](https://app.buildbuddy.io/invocation/b21b45ad-b87a-5b71-bb11-896b63ddafd9)
reused nine passing cached results and physically reran the six failures. Four
timed out again on fresh `sjc-*` VMs: three had not logged PostgreSQL import
completion, and one finished import in 40.4 seconds and had not reported
PostgreSQL readiness. Of the other two, `test_codex_title_conflict` passed on a
cold `gke-*` VM after a 12.9-second import. `test_codex_titles` passed after
restoring a VM last used by the earlier #15560 CI invocation
`8df85c24-4172-5e04-8502-9ef72438a011`; its log contained no image import.
This demonstrates reuse across targets, but neither attempt establishes a
fleet-wide hit rate or reliable cold-start behavior.

## Cold import profiles

As a diagnostic, `test_database_migrate` ran with a temporary `--run_under`
wrapper collecting system-wide `perf` samples at 49 Hz for the first 35
seconds. It used the same RBE image and the actual test, but altered the action
and recycling configuration to obtain cold VMs. Two `gke-*` runs imported
PostgreSQL in [8.2](https://app.buildbuddy.io/invocation/3e15c7a3-a540-4ce5-9dd2-e1a481a61ad9)
and [8.4](https://app.buildbuddy.io/invocation/b92b865e-6e19-4bc5-9b24-a7cdfa89fdea)
seconds; two `sjc-*` runs took
[27.2](https://app.buildbuddy.io/invocation/74f8c1bf-56da-4b1a-89d2-6c893b6b22dd)
and [23.2](https://app.buildbuddy.io/invocation/3daa8ed5-4967-43f4-9e54-5a02a6b35686)
seconds. All four tests passed. The `sjc-*` probes used a distinct local-only
recycling key; they did not enable remote snapshots.

A further [diagnostic invocation](https://app.buildbuddy.io/invocation/442f578b-f08b-45ee-9852-be7d7bad3278)
ran the four repeat failures with the profiler, a separate local-only family,
and a temporary 180-second test limit. All passed; `test_json_storage` took
61.6 seconds on a cold `sjc-*` VM, including the profiling wrapper, with
pytest reporting three cases passed in 42.3 seconds and a 20.7-second image
import. This instrumented pass is not a measurement of an unmodified action
crossing its usual deadline and does not justify raising the committed limit.

In the kernel-inclusive `sjc-*` profile, a sampled stack for Docker included
buffered ext4 writes and `copy_user_enhanced_fast_string`; user-mode samples
also observed `unpigz`. In the 17.8-second sampled window containing `unpigz`,
1,340 non-idle samples included 992 in Docker and 323 in `unpigz`. Samples are
not wall-time attribution, and the window does not cover the entire import.
These profiles show image unpacking and filesystem writes, but do not identify
why the observed host groups differed or whether the timed-out imports were
making progress at their deadlines. Provider-side CPU and I/O visibility during
a failing action would be needed to investigate that boundary. The earlier
[loader comparison](../../debug/2026_10_27_oci_loader_comparison.md) did not
establish a faster replacement for the existing loader.

## Follow-up guest CPU and disk observations

On 2026-11-11 UTC, two more cold runs of the real `test_database_migrate`
target used source `1459dc9fb63d362d248bed98fad833d12f926c43`, with an
instrumentation wrapper and distinct temporary local-only recycling keys. Both
passed and landed on `sjc-*` hosts. The wrapper sampled guest `/proc/diskstats`
and `/proc/stat` at roughly 0.5-second intervals; the first run also captured
kernel-inclusive `perf` samples at 49 Hz. These are instrumented runs, not a
representative fleet sample.

The following counter deltas cover the sampled intervals enclosing each logged
PostgreSQL import. Disk counters are guest-wide, and active CPU, I/O-wait, and
steal core-seconds sum across six guest vCPUs; none is an attribution solely to
PostgreSQL. BuildBuddy execution metadata reports `/dev/vda` mounted at `/` and
`/dev/vdb` mounted at `/workspace`; the probe did not capture its own mount table.

| Invocation                                                                            | Import | `vda` writes (`/`) | `vdb` reads (`/workspace`) |   Active CPU |    I/O wait |       Steal |
| ------------------------------------------------------------------------------------- | -----: | -----------------: | -------------------------: | -----------: | ----------: | ----------: |
| [98e6f327](https://app.buildbuddy.io/invocation/98e6f327-4c37-43e4-9de0-ef2d953656ab) | 14.7 s |            446 MiB |                    155 MiB | 22.00 core-s | 1.37 core-s | 1.43 core-s |
| [8d31c267](https://app.buildbuddy.io/invocation/8d31c267-07dc-49cd-a960-47e95f89ba94) | 15.5 s |            534 MiB |                    155 MiB | 23.82 core-s | 2.19 core-s | 7.84 core-s |

Across the **whole test actions**, BuildBuddy reports CPU pressure `some` totals
of 0.75 and 2.55 seconds, and I/O pressure `some` totals of 0.089 and 0.099
seconds. These are not aligned to the import windows. CPU pressure records
runnable wait, not cgroup throttling.

The first run's perf capture reported no lost samples. During its approximately
12-second `unpigz` window, 641 samples were in Docker and 156 in `unpigz`;
Docker stacks included `copy_user_enhanced_fast_string`, `generic_perform_write`,
and `ext4_buffered_write_iter`. This shows CPU work in Docker's unpack and guest
filesystem-write path. On-CPU samples and aggregate guest I/O-wait core time do
not establish how much import wall time was spent on decompression, writing, or
off-CPU waits; they cannot rule out a serial disk stall or expose provider
disk latency.

These two runs do not explain the earlier GKE–SJC timing difference: both used
SJC, and the earlier GKE and SJC profiles had different kernel coverage.
Synchronized provider-side host CPU and backing-disk metrics, or an identically
instrumented cold run with controllable GKE placement, would be needed to
separate those explanations. No remote snapshot sharing or save-policy change
was used.
