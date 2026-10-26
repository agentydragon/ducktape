# GitLab startup: CPU work and stalled image reads

## Findings

GitLab has two distinct startup costs. Warm application loading/initialization uses
roughly 80 seconds of CPU on the measured nodes. Cold image-file reads from a busy
rotational disk can add minutes before Rails initialization even begins. Probe
failures and Helm rollbacks then repeat that work.

A boot-only reproduction on `ovh-ns103656` was interrupted after **491.22 seconds**,
having used only **7.40 CPU seconds**. It was still in `Bundler.require`, loading
ActiveRecord dependencies, before `Rails::Application#initialize!`. Repeated
snapshots found the Ruby main thread in disk sleep at `folio_wait_bit_common`, with
Ruby source and Bootsnap bytecode files open. This establishes filesystem waiting
as the dominant delay in that reproduction, rather than merely correlating boot
time with node-wide disk activity.

This concerns the **container image filesystem**, not the Gitaly repository PVCs.
No application placement, storage, probe, or node configuration was changed.

## Experiment and accounting

Used the exact deployed `gitlab-webservice-ee:v19.5.1` image, manifest digest
`6a74e05a2e16446d6e5394d33aa1cef22279541092132bddeff43cc73658ae13`, and the
deployed Puma configuration. The reproduction calls
`Puma::CLI.new(...).launcher.config.app`: it loads the real Rack application but
does not call `run`, bind listening sockets, or fork request workers. Application
initializers and the configured Rack warmup still execute.

The first run was a separate process inside an existing healthy app Pod. Two
disposable Pods then used the same image, init containers, configuration mounts,
and resource settings on the selected HDD and SSD nodes. Their labels matched no
serving Service. A temporary toleration allowed only the SSD diagnostic Pod onto
the selected control-plane node. Each run used a private metrics directory.

GNU `time -v` measured the whole command. StackProf 0.2.28 sampled application loading
in wall mode at 1 ms. The existing security policy denied `strace` even for a child
process; no capabilities or security policy were changed. Kernel `/proc` counters,
wait channels, scheduler counters and open file descriptors supplied the I/O evidence.
The completed timings include profiler overhead.

| Run                                           | Node           | Observed wall time | CPU user + system | Outcome                                   |
| --------------------------------------------- | -------------- | -----------------: | ----------------: | ----------------------------------------- |
| Existing warm app filesystem                  | `ovh-ns102453` |            87.57 s |           80.50 s | Completed                                 |
| Fresh diagnostic container on NVMe node       | `ovh-ns104963` |            81.25 s |           76.59 s | Completed                                 |
| Fresh diagnostic container on busy HDD worker | `ovh-ns103656` |           491.22 s |            7.40 s | Investigator interrupted; boot incomplete |

The NVMe image had just been pulled/unpacked, and its measured boot reported zero
physical `read_bytes`. This is **not** an isolated cold-SSD versus cold-HDD benchmark:
cache state and CPU hardware differ. No shared caches were flushed. The HDD result
is a lower bound on completion time, not an eight-minute successful boot. Its
interruption also prevented StackProf's normal final report from being written;
the completed warm and SSD reports are retained.

## Why bytecode cache hits still involve disk reads

The image enables Bootsnap with `BOOTSNAP_CACHE_DIR=/srv/gitlab/bootsnap`.
The warm run recorded **32,349 hits and no misses/stale entries**. Its whole-process
I/O counters still reported approximately 150 MiB of logical reads and 140,000 read
syscalls across startup. These counters are not all Bootsnap operations.

In [Bootsnap 1.26.0's native implementation](https://github.com/rails/bootsnap/blob/v1.26.0/ext/bootsnap/bootsnap.c),
`bs_fetch` opens/stats the source file, opens the compiled cache file, reads its key,
validates it, and reads/deserializes cached bytecode. A valid compiled artifact
avoids compilation; it does not guarantee that file contents or metadata are in
the Linux page cache. Cold source/cache lookups therefore remain sensitive to
rotational-disk latency even with a near-100% hit rate.

The measured worker's root OverlayFS lives under containerd's `/var` directory.
Node-exporter identifies that filesystem as XFS on **`/dev/sda5`**, backed by an
HGST HUS726020ALA610 rotational disk. The SSD control's `/var` is XFS on
`/dev/nvme0n1p7`, backed by Intel NVMe.

Between 00:26:49 and 00:28:51 UTC, the HDD reproduction accumulated only **0.993 s**
of main-thread CPU execution and **0.171 s** of scheduler runqueue delay over a
122-second observation interval. Both snapshots found it in disk sleep; physical
reads increased from 4.69 to 11.90 MB. It was progressing through file loading,
not consuming those minutes executing Ruby or waiting to be scheduled on a CPU.

## Contention and the historical limit

During the reproduction, five-minute node rates showed `sda` **90.6% busy** and
**96 ms mean read latency**. The largest container reader was
`agentplane-staging/virt-launcher-finance-agent-vm-1-wdh49`'s compute container,
reading approximately **100 MB/s specifically from `/dev/sda`**. GitLab's diagnostic
container was reading only tens of KiB/s. These are overlapping physical-device
and container attribution metrics; they must not be added together.

The VM's burst did **not** occur at the same rate during every earlier failure.
At 20:48 the largest `sda` container reader was `virt-handler`; at 22:30 GitLab itself
was the largest reader and Flux's source-controller was writing approximately
2 MiB/s to `sda`. Slow startup therefore cannot all be assigned to the current VM.
The common vulnerability is many image-file lookups on a shared rotational root
disk. The new reproduction proves that path can dominate elapsed boot time; it
does not reconstruct each killed historical attempt or assign every historical
millisecond to a specific competing workload.

## What the warm CPU work does

The completed NVMe run allocated approximately **47.4 million Ruby objects**,
retaining 7.0 million slots at the final snapshot. Native GC accounting reported
**11.55 seconds** spent collecting. StackProf attributed 83.6% of sampled stacks
to `Kernel.require`, 77.2% to Rails initializer execution, 22.4% to Zeitwerk eager
loading and 16.0% to GC. The nested frames overlap and must not be summed.

Visible initialization includes API/routing construction, model/class metadata,
metrics setup and enterprise-module injection. `zz_metrics.rb` includes nested
API loading; its inclusive samples are not the cost of metrics alone. PostgreSQL
`exec_params` accounted for 5.5% of self samples in that completed run, not a
multi-minute database wait.

## Next intervention and cleanup

The targeted intervention to evaluate is an SSD-backed application image/runtime
filesystem for Rails and Sidekiq. That is independent of keeping Gitaly Git data
on its current local HDD volumes. Less image-disk contention also helps, but warming
code or raising probe budgets does not establish reliable cold starts.

Both diagnostic Pods were deleted, the production Pod's temporary tooling/profile
files and private metrics directories were removed, and the Mimir port-forward was
stopped. Both serving app Pods remained Ready with their original restart counts
(one and zero); the investigation did not restart them.

[Native profiler reports, GNU time output, process counters and metric captures](results/2026-10-26-startup-io/)
retain the evidence. Binary profiler dumps and raw logs remain outside git in
`/tmp/gitlab-startup-forensics-2026-10-26`.
