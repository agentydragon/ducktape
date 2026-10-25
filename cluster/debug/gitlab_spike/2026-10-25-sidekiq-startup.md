# Sidekiq tuning: startup retries and memory, 2026-10-25

## Conclusions

- The long rollout was repeated startup attempts, not an observed successful
  five-minute-plus boot. Default liveness checks killed the cold worker while Rails
  was still initializing. Helm subsequently rolled the upgrade back.
- The first attempts had direct kernel evidence of filesystem page waits. The image
  filesystem is on rotational HDD, and node disk latency and I/O pressure increased
  during the failed starts. This supports cold filesystem I/O contributing to slow
  initialization; it does not quantify all of the failed attempts' elapsed time.
- Subsequent workers completed native application boot in **76.82 s** and **55.36 s**.
  Both reported 32,470 Bootsnap hits, one miss and 11 stale entries. Compiled-cache hits
  do not establish that its files are already in the operating system's page cache.
- A real StackProf profile of a warm boot shows Rails code loading/initialization
  dominating, without a sampled multi-minute external wait. It is not a profile of
  the earlier failed cold attempts.
- Capping Sidekiq at two replicas controls expansion under load. Lower concurrency
  did not produce a large reduction in quiet-period memory. A fresh-process comparison
  cannot attribute the observed small difference solely to concurrency.

## Deployment and failure sequence

All times are UTC. PR #12041 merged at 20:42:34 as
`8344b8572835aacd58055a1787f7a2736e1e4978`, setting maximum replicas to two and
concurrency to ten. Minimum replicas remained one.

The first replacement ran on `ovh-ns103656`. Its original liveness configuration
was initial delay 20 s, period 60 s, failure threshold three. Kubernetes events
explicitly recorded liveness-triggered termination. One attempt started at 20:44:26
and ended at 20:47:13; the next reached queue configuration, Redis setup and SLI
preloading before another termination. `ps` observed the main Ruby worker in an
uninterruptible `folio_…` kernel wait during initialization. Old workers remained
Ready throughout the rolling replacement.

The Deployment stalled, and Helm recorded a successful rollback to release v2
at 20:54:12. A subsequent retry completed with the shorter probe settings. PR #12043
then merged as `2d20e86fbb568de06be8712b126d481ef04be314`, increasing initial
liveness delay to 300 s. This extends the startup budget; it does not fix slow I/O.
Readiness remained immediate and the steady-state liveness cadence was unchanged.

The final rollout completed as Helm release v6. Both final Pods had zero restarts:

| Node           | Container started | Native application boot finished | Native boot duration |
| -------------- | ----------------- | -------------------------------- | -------------------: |
| `ovh-ns103711` | 21:00:45          | 21:02:07.761                     |              76.82 s |
| `ovh-ns102453` | 21:02:38          | 21:03:37.645                     |              55.36 s |

GitLab's native `Gitlab::Metrics::BootTimeTracker` measures application process boot;
these values exclude init containers, and readiness can follow application boot.
The deployed `config/initializers/sidekiq.rb` starts the health server in Sidekiq's
`:startup` callback. `Sidekiq::CLI#run` fires that callback after application boot
and warmup. Refused health connections during initialization therefore do not prove
that an already running worker is wedged.

## Filesystem evidence and limits

The container root is OverlayFS whose lower and upper directories are under
`/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/`. On the affected node,
node-exporter identifies `/var` as XFS on `/dev/sda5`, and `sda` as a rotational HGST
HUS726020ALA610 HDD. These are application image reads, separate from the Gitaly PVCs.

Historical node metrics, using five-minute rates because shorter windows lacked
sufficient scrape samples, showed the following during the failed starts:

| UTC   | `sda` busy fraction | Mean read time | Node I/O waiting pressure |
| ----- | ------------------: | -------------: | ------------------------: |
| 20:46 |               38.9% |        17.3 ms |                      9.7% |
| 20:47 |               57.1% |        38.9 ms |                     20.6% |
| 20:48 |               70.8% |        33.3 ms |                     23.8% |
| 20:49 |               79.5% |        27.7 ms |                     26.3% |

These are node-wide observations, not per-process attribution. Snapshot node memory
usage was 45%, so that snapshot does not suggest node memory exhaustion. No CPU
limit was configured for the Sidekiq container. Kernel page waits establish that
I/O occurred on the critical path, but a retrospective profile cannot assign the
entire cold-start delay to those waits or establish which workload caused contention.
Do not present the longer liveness delay as resolving that attribution gap.

## Real boot profile

A `kubectl debug` attempt to attach `strace` with `SYS_PTRACE` was rejected by the
namespace's baseline PodSecurity policy. No security setting was weakened.

Instead, used the image's existing **StackProf 0.2.28** to profile the exact deployed
`Sidekiq::CLI#boot_application` method in a separate process in a Ready Pod. Source
inspection confirmed that the method loads Rails; `run`/`launch`, startup callbacks
and job processors were not invoked. Application initializers still ran, including
idempotent Redis cron configuration. The profiling process exited at 21:08:40.

The wall profile used a 1 ms interval: 80,039 samples, 6,960 missed samples. Results:

| Frame or category            | Sample fraction | Interpretation                                  |
| ---------------------------- | --------------: | ----------------------------------------------- |
| `Kernel.require`             | 85.4% inclusive | Ruby/GitLab loading and nested initialization   |
| Rails `run_initializers`     | 78.9% inclusive | Includes eager loading and other initialization |
| Zeitwerk `eager_load`        | 20.0% inclusive | Subset of the above                             |
| Garbage collection           |           14.4% | Marking and sweeping                            |
| `PG::Connection#exec_params` |       5.2% self | Database calls also nested under initialization |

Inclusive entries overlap and must not be added. The profile covers a later, warm
boot-only reproduction, not the full worker startup or any killed process. Its
8% missed-sample rate and profiling overhead further limit precise wall-time estimates.
The [native profiler report and phase frames](results/2026-10-25-sidekiq-tuning/)
are retained; the 4.8 MiB binary dump remains outside the repository.

A useful next controlled experiment would compare this same boot-only profile on
an SSD-backed application filesystem. Do not flush shared node caches or move
Gitaly repository data to create a cold-cache test.

## Memory and queue recheck

The quiet-period comparison below uses the same server namespace before and after
rollout. It excludes shared SeaweedFS and the Runner namespace. No benchmark workload
was started for this recheck; normal periodic GitLab background jobs continued.
All after-sample windows begin after the profiler exited. Its CPU burst can still
influence the HPA's five-minute scale-down stabilization window.

| Quiet-period medians    | Before: concurrency 20 | After: concurrency 10 |
| ----------------------- | ---------------------: | --------------------: |
| Entire GitLab namespace |              11.91 GiB |             11.70 GiB |
| Sidekiq combined        |               3.34 GiB |              3.16 GiB |
| Sidekiq Pods            |                      2 |                     2 |
| Samples                 |                      3 |                    10 |

Before captures: `20261025T204219Z`–`20261025T204300Z`; after: `20261025T210950Z`–`20261025T211407Z`.
Observed difference: 0.21 GiB (1.7%) total and 0.17 GiB (5.2%) Sidekiq.

Both periods observed two Sidekiq Pods. The baseline workers were long-lived; the
replacement workers were fresh. This is not a matched-load measurement or an idle
floor. The earlier **15.31 GiB** total / **6.35 GiB** Sidekiq medians were measured
under the paired benchmark with up to four workers and remain a separate result.

Sampled queues stayed empty with zero reported queue latency; retry count remained
zero and the cumulative failed-job count remained 76, already present before rollout.
The dead set contained 14 pre-existing entries. These observations establish light-load
operation, not capacity at the former benchmark rate.

[Per-Pod memory, queue samples and summary](results/2026-10-25-sidekiq-tuning/)
retain capture times and underlying metric timestamps/windows. Raw container and node
captures remain in `/tmp/gitlab-sidekiq-cap-evidence` and `/tmp/gitlab-sidekiq-forensics`
on the investigation host.

## Inside one Sidekiq Ruby worker

A later `/proc` snapshot measured the live worker at **1,475 MiB PSS**, including
**1,457 MiB private anonymous memory** and approximately **13 MiB proportionally
counted file-backed memory**. The supervisor, metrics server and logger added
approximately **73 MiB PSS combined**. These process measurements are separate from
cgroup/kernel accounting and the earlier namespace memory samples.

Ruby heap introspection was not enabled in the running worker. A separate boot-only
reproduction used Ruby's native `ObjectSpace` inspector after a full GC, without
starting job processors. An initial allocation-traced attempt added approximately
512 MiB of tracing metadata; its sizes were discarded. The final reproduction did
not enable allocation tracing and used a private Prometheus metrics directory.

| Ruby object category                                                        | Reported size |
| --------------------------------------------------------------------------- | ------------: |
| VM internal objects: methods, bytecode, environments and caches (`T_IMEMO`) |       429 MiB |
| Class/module/mixin metadata (`T_CLASS`, `T_MODULE`, `T_ICLASS`)             |       132 MiB |
| Hashes                                                                      |       118 MiB |
| VM/C-backed data objects (`T_DATA`)                                         |       108 MiB |
| Strings                                                                     |       101 MiB |
| Ordinary objects                                                            |        43 MiB |
| Arrays                                                                      |        40 MiB |
| Regular expressions                                                         |        20 MiB |
| Other types                                                                 |         5 MiB |
| **Total reported live-object memory**                                       |   **996 MiB** |

Internal record counts include approximately 392,000 instruction sequences, 550,000
method entries, 842,000 call caches and 248,000 execution environments. These are VM
records, not counts of source files or distinct application features. There were
approximately 1.13 million strings and 421,000 hashes in the boot snapshot.

Identifiable shallow object families include 10,212 GraphQL fields (6.23 MiB),
8,285 GraphQL arguments (2.12 MiB), 279,881 Procs (21.35 MiB), and 168,678 unbound
methods (12.87 MiB). Routing, model metadata and callbacks are also visible. Their
associated strings, hashes, classes and code appear in the shared type buckets;
these shallow figures are not whole-feature memory budgets.

The native counts establish a large baseline before any jobs are processed. They do
not reconcile every byte of the older live worker's RSS: ObjectSpace reports object
storage/payloads, with incomplete accounting for some native allocations, and excludes
free heap capacity. The boot-only process had a different lifetime and was explicitly
garbage-collected. No full heap or string contents were exported.

Boot diagnostics also create multiprocess metrics files under the fallback `sidekiq`
identity when not isolated. Those files from the stopped diagnostic processes were
removed only after checking their filename and creation time; the real `sidekiq_0`
worker files were preserved. Temporary census files were removed after retaining
safe aggregate results.

[Aggregate object counts, class sizes and live process memory](results/2026-10-25-sidekiq-heap/)
retain the evidence. The bytecode/class-heavy baseline explains why reducing job
concurrency alone produced a small memory difference, while independent Pod replicas
each carry most of that baseline.
