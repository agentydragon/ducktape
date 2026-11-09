# HIL KVM worker CPU and scratch experiment

Retain **4 vCPU / 16 GiB** for this single-action worker shape. Eight CPUs were
available and used, but did not improve the measured critical path. This is a
bounded sizing result on one shared HIL host, not a universal CPU scaling claim.
RAM stayed fixed; no smaller-memory configuration was validated.

The [compact evidence](2026_11_09_hil_kvm_worker_canary.json) records source, physical
executions, native profile hashes/URIs, input identity, resource allocation, health limits and cleanup.
The [opt-in renderer](../kvm/x/canary/README.md) creates one manually owned worker
outside Flux and production selection. It defaults to Halted and requests the
full 80 GiB scratch budget. Production worker/pool/cache/KEDA/node settings were
not changed.

## Matched CPU measurements

All six runs passed the existing full Firecracker contract at
`de9e7c656c00a1dc1e758101bf7dd8009c3d57ae`. Guest RAM 16 GiB,
work disk 80 GiB, one action slot, image pins and action memory reservation 10 GiB stayed fixed. The explicit action CPU request changed
with the VM: live scheduler allocation 4000/8000 mCPU. Docker had no CPU quota;
the eight-CPU action's allowed CPU list was 0–7. It peaked above four busy CPUs.

Kubernetes selected `ovh-ns1001419` for both guest lifetimes. The configuration
uses only zone `hil-ovh` (region `hil`), with no hostname constraint. Both used
physical WDC SN720 NVMe scratch and identical Ubuntu/launcher image digests.
Decoded Action and Command differed only in platform `EstimatedCPU=4/8`;
arguments, environment, outputs and input-root digest matched exactly. The CPU
property is duplicated in Command, so its digest differs as expected.

| Pair / vCPU | Worker filecache |                                                         Command seconds / invocation | Fetch s | Setup s | 26 serial imports s | Observed cgroup peak GiB |
| ----------- | ---------------- | -----------------------------------------------------------------------------------: | ------: | ------: | ------------------: | -----------------------: |
| 1 / 4       | cold             | [278.345](https://app.buildbuddy.io/invocation/867d788d-2430-4169-9ffa-4c1557fb776c) |  12.283 | 120.291 |              69.449 |                     6.51 |
| 1 / 8       | cold             | [280.706](https://app.buildbuddy.io/invocation/37ba5347-411b-4045-95ff-89798015dc9e) |  12.956 | 124.947 |              71.829 |                     6.69 |
| 2 / 4       | warm             | [250.283](https://app.buildbuddy.io/invocation/2ea5b83f-df7e-4139-9d5b-9723b5604953) |   0.481 | 120.213 |              67.504 |                     7.65 |
| 2 / 8       | warm             | [259.467](https://app.buildbuddy.io/invocation/42bad75a-1d02-4beb-8b24-dd7870d1bc8f) |   0.215 | 124.979 |              71.610 |                     8.68 |
| 3 / 4       | warm             | [251.844](https://app.buildbuddy.io/invocation/611e9676-fbf4-4a4b-9d30-8d529d35f4f9) |   0.251 | 119.186 |              66.495 |                     8.30 |
| 3 / 8       | warm             | [276.572](https://app.buildbuddy.io/invocation/6bb7290f-2f1a-417d-8ffa-15e113f05c14) |   0.211 | 122.904 |              66.995 |                     8.09 |

Warm means were 251.064 s at 4 CPU and 268.020 s at 8 CPU. There are only three
matched-state pairs, ordered 4 then 8 in two recreated guests. Shared-host load,
SMT/frequency and order effects remain possible explanations for small
slowdowns. The result supports retaining 4 CPU; it does not establish an inherent
penalty for 8 CPU.

The extra CPUs relieved guest run-queue pressure and raised brief CPU peaks,
but setup stayed dominated by serial imports and service readiness. Existing
15-second containerd CPU profiles had SHA256 at 46.43%/45.06% of sampled CPU on
the first 4/8 CPU runs. The suite also preserves its intentional idle interval,
live guest, history, credentials, mounts, checkpoint/resume and cleanup checks.
Whole-action CPU averaged 1.14–1.31 busy cores at 4 CPUs and 1.33–1.43 at
8 CPUs. Guest CPU PSI some fell from 9.85–11.73% to 2.86–3.35%, while I/O PSI
full remained 2.66–4.30% across the 8-CPU runs (2.78–3.97% at 4 CPUs). The work
disk averaged 54.5–60.3 MiB/s writes and 12.9–18.1% busy across the six runs;
write IOPS-weighted await was 9.1–21.0 ms. These are whole-action guest-device
observations, with zero-device intervals included; they do not identify a
physical-disk intervention. The native archives retain phase/process CPU,
pressure and block-device samples.

No fixture coverage, deadline or command was reduced for CPU measurements.

## RAM and clock interpretation

Live cgroup memory high-water marks include reclaimable cache. Full warm-run
`sar` data retained substantial available guest memory; see the JSON for each
minimum and CPU/memory-pressure series. No sampled cgroup OOM/throttling event
was observed. RAM stayed at 16 GiB; reducing it requires separate validation.

Docker removes action cgroups before a waiter can read final held-open counters;
those reads returned ENODEV. The table uses live observed high-water counters,
with the final sample's distance from action end retained in JSON. CPU snapshots
have the same end-boundary limit. Native whole-guest CPU includes executor,
kernel and profiler overhead. Cold 4 CPU has no sar memory series; its host PSI
starts 72 s into the action. Candidate sar coverage is partial.

Command/fetch durations use same-worker execution timestamps. Upstream
`fileDownloadDurationUsec` included process time, and cross-host queue clocks
showed about one second of skew; neither is used as elapsed fetch/queue time.
Nested or concurrent phase spans are not added to total elapsed time.

## Storage boundary and fixture control

A matched HDD/SSD intervention was unavailable. The three feasible HIL compute
hosts had NVMe scratch and about 124 GiB physical free space before admission.
The HDD hosts had only 2–3 CPU and 7–8 GiB request headroom, below the fixed shape.
The original wyrm2 SSD pool had only 49.63 GiB free, below 80 GiB. No data was removed
or storage expanded. Historical HDD-worker timings versus HIL NVMe change
host class, contention and source, so they do not quantify a storage speedup.
A disk comparison needs separately reviewed capacity/storage that can admit
matched arms while preserving unpinned HIL placement; record actual placement
and stratify if it changes.

The separately prepared PR #15099 fixture candidate at
`9943dc0330fb6c4f3bf49dd4cb5e9d1686ad0a14` passed on 4 CPU in 214.110 s. In the
A–B–A sequence, setup was 120.291→76.807→120.213 s and serial image imports
were 26/69.449→20/37.941→26/67.504 s. Scenario time varied independently, so the
whole cold-run difference is not attributed to the fixture. The CPU experiment
kept the untrimmed source fixed; post-merge 8-CPU behavior was not measured.

## Health and teardown

An 8-CPU guest watchdog warning occurred at 10:45:42 UTC, after the
second 8-CPU run ended and while serial profile extraction was running. Its timer/softirq trace interrupted
an idle task. Host CPU pressure also spiked during that interval; guest/host
checks recovered, and the cause remains unproven. The final run is explicitly
a post-warning sample. No node tuning or extra reproduction demand was added.

All owned demand was reconciled to terminal physical executions and unique
worker identities. Exporter completion counts matched the ledger, assigned and
queued resources were zero, and Docker/K3s/process/mount checks were idle.
The executor then logged successful graceful stop and registration cancellation.
Its service was inactive/success before stopping the VM. VMI/launcher deletion
preceded the CPU update, so two canaries never coexisted. Final cleanup deleted
only the three original owned object UIDs, verified absence, checked reclaimed
scratch and production identity/readiness; the JSON contains the receipt.
No executor-admin credential was needed.

Native diagnostic archives were allowlisted before authenticated BuildBuddy
CAS upload: sysstat, PSI, owned action counters/lifecycle only. Journal, console,
configuration and environment files are excluded. Every uploaded archive was
downloaded and its SHA256/size checked. Retrieve with `bb download HASH/SIZE`
from the repository Nix devshell; hashes and bytestream URIs are in JSON.
CAS retention follows the existing cache policy; the compact measurements are
committed here independently of cache lifetime.

Rendering both CPU configurations, Kubernetes server dry runs, the explicit
renderer-library mypy/Ruff build and pre-commit passed during preparation. Both native KVM smoke
actions and all full measurements passed uncached. Hosted PR checks remain a
separate gate; at measured head de9e7c, ordinary `test_vm` failed waiting for
`llm-ingress` readiness on its default pool. These canary passes do not make that
hosted run green or deploy a production worker change.
