# KVM worker placement, 21 November 2026

Keep `wyrm2` eligible for the production `kvm-vm` pool. Revisit placement if
guest teardown failures recur enough to affect CI. The
[pool runbook](../../../../cluster/k8s/buildbuddy-kvm-worker/README.md) owns the
production configuration and operating procedure.

[PR #18613](https://github.com/agentydragon/ducktape/pull/18613) recorded the
live expansion of wyrm2's Piraeus SSD backing disk from 150 to 600 GiB.
[PR #18615](https://github.com/agentydragon/ducktape/pull/18615) removed the
OVH-only node selector. The nested-KVM requirement and the node-local
`piraeus-ssd-local-delete` work claims still constrain placement. Live inspection
on 21 November found `buildbuddy-kvm-worker-1` Ready on `wyrm2` with a bound
`work-1` SSD claim. Workers on OVH nodes were also Ready.

## Pinned SSD comparison

Both workers ran `//agentplane/system_tests/x/vm_kvm:test_vm_kvm` at
`0d1f154903b9e9c58b1c7fb5f4484651b800caf2`. Each run built a fresh K3s fixture
on a worker with warm caches, with the action pinned to a registered executor
ID. The table uses the TestRunner's `executionStartTimestamp` through
`executionCompletedTimestamp`. Queueing, input fetch and output upload are
outside that interval. The production suite's retained fixture needs its own
measurement.

| Run             | OVH control plane (`ovh-ns104963`)                                                          | `wyrm2`                                                                                               |
| --------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Pair 1          | [207.97 s, pass](https://app.buildbuddy.io/invocation/ea50b356-e020-4547-a60d-c135ceffc497) | [369.01 s, pass](https://app.buildbuddy.io/invocation/2cb2fa36-535d-4b57-9ccc-7e0ee1f754e2)           |
| Pair 2          | [207.70 s, pass](https://app.buildbuddy.io/invocation/7b2be7f7-3949-4d55-aa2c-bad8ac3dc917) | [355.85 s, teardown error](https://app.buildbuddy.io/invocation/e03dc565-cb69-42df-a7fc-cbc10be52e05) |
| Profiled repeat | —                                                                                           | [336.52 s, pass](https://app.buildbuddy.io/invocation/3f17c33a-f4f6-42e9-889e-8a40a400463f)           |

Another worker booted on `wyrm2` during pair 1. Pair 2 ran without that boot.
Both scenario test cases passed in pair 2 before teardown failed.

Pair 1's two guest consoles took about 10 s on the OVH node and 32–33 s on
`wyrm2` from the first Linux line to systemd. Guest readiness took 42–45 s and
116–120 s respectively. Shutdown request to completion took about 4 s and
13–25 s respectively. The consoles buffer output. Both hosts ran the nested
guests with KVM. The bare-metal Intel control plane runs the KubeVirt worker
directly. The AMD Atlas host runs `wyrm2` in Proxmox, adding a virtualization
layer before the KubeVirt worker. Five-minute node I/O pressure rates were
roughly 0.2% on the control plane and 2.6–4.9% on `wyrm2`, including other
workloads. These measurements do not isolate the effects of storage, CPU,
nesting or concurrent load.

## Guest shutdown

In the [failed pair-2 run](https://app.buildbuddy.io/invocation/e03dc565-cb69-42df-a7fc-cbc10be52e05),
`systest-vm-1` (VMI UID `1fc2083d-191f-4a95-a0af-02bf3dfbd226`) received the
shutdown request at 11:44:31 UTC. Its retained console and raw serial have
neither `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE` nor kernel powerdown. The
UID-correlated `virt-handler` log records grace-period expiry and a kill at
11:45:01.286 UTC, then `Shutoff reason Destroyed` at 11:45:02.141 UTC.
The sibling guest emitted the marker, powered down, and reached
`Shutoff reason Shutdown`. The artifact paths are under
`test.outputs/vm-artifacts/{consoles,serial,handlers}/`. What held up the first
guest remains unknown.

Seven manual `test_vm_kvm` invocations at the pinned commit produced ten
uncached TestRunner executions. The census joined their BuildBuddy executor
host IDs to Kubernetes node names in the workers' Loki console streams.
Nineteen of twenty nested guests emitted the marker in both console and raw
serial, logged kernel powerdown, and had a corresponding `Shutoff reason Shutdown`.

| Worker node     | Passing actions | Actions with a forced guest stop | Additional runs                                                                                               |
| --------------- | --------------: | -------------------------------: | ------------------------------------------------------------------------------------------------------------- |
| `wyrm2`         |               3 |                                1 | [Earlier fresh-fixture pass](https://app.buildbuddy.io/invocation/9f0998bf-19a9-48a5-9aa8-cd93cd6ee407)       |
| `ovh-ns104963`  |               4 |                                0 | Two passes in the [four-run burst](https://app.buildbuddy.io/invocation/3b655597-26e4-4f0a-b458-d97eabcf83d6) |
| `ovh-ns1001419` |               2 |                                0 | Two passes in that burst                                                                                      |

The manual benchmarks disabled BuildBuddy target tracking. From 09:00 to
13:20 UTC on 21 November, explicit enumeration of Ducktape test invocations
found the seven manual children above and 63 children with a required
`//agentplane/system_tests/suites:test_vm` result. All 63 required-suite
results were cache hits. The fresh-fixture benchmarks give no production
failure-rate estimate by node.

The [17 November mixed-burst trial](https://app.buildbuddy.io/invocation/90d926d2-0b20-42a1-a3cd-11a9ef25a04d)
had two earlier teardown failures on separate workers on `ovh-ns104952`.
Both scenarios passed in each failed action. Across runs 3 and 4, three guests
lacked the marker and kernel powerdown. The matching `virt-handler` records
show grace-period expiry followed by `Shutoff reason Destroyed` for each.
The trial used `test_vm_kvm_recycled` with cold fixtures and a different
source commit. It establishes that the failure predates the placement change.

## Matched SSD and HDD work disks on wyrm2

Keep the production pool on SSD work disks. With fresh worker disks, the HDD VM action took 1,457.80 s versus 518.36 s on SSD (2.81×). With warm worker caches and a fresh fixture in each arm, it took 672.46 s versus 344.88 s (1.95×). This is one sample per medium and cache state. Required CI's retained fixture and PR feedback time remain unmeasured for this media comparison.

The trial used one active canary at a time. Both arms ran on `wyrm2` with
the production NixOS containerDisk
`sha256:8490616944d9817efd3a756a22ad54824639e3d615c7fd3de76deb9b053a0268`,
four vCPUs, a two-CPU request, 8 GiB guest RAM and one executor slot.
Each arm had its own 80 GiB ext4 work disk in a Filesystem claim. CDI requested
`88919245Ki` on both volumes. LINSTOR showed a single local diskful copy in
`ssd` (`piraeus-ssd-local-delete`) or `hdd` (`piraeus-hdd-local`).
Wyrm2 runs under Proxmox. Its [Piraeus backing zvols](../../../../cluster/docs/piraeus.md#nixos-worker-pools)
use NVMe and four-disk RAIDZ2.

The local source was `0d1f154903b9e9c58b1c7fb5f4484651b800caf2` with
`pool = "kvm-work-disk-trial"` for the tested label
`//agentplane/system_tests/x/vm_kvm:test_vm_kvm`. The cold-fixture action has the same input root and command as the prior placement
comparison after excluding its `Pool` property. Native execution receipts
join the physical TestRunner to the executor host ID from its guest
journal. The first SSD run used the dedicated one-member pool. Subsequent
runs also pinned the current executor registration ID. The BuildBuddy
outer runner's recorded commit refers to the remote checkout onto which
it applied the local Git-state patch.

All four runs resolved to TestRunner action
`97ee70e0e47d4c73b042cb96cd881728a36e9c5e3f360cf9dea7297c593223a3`
with input root
`9e205c54e1108565210af137c58f6bca91ce29e3651a9c1de337f41b67059c4c`.
The action container used
`sha256:c73af98850208e2a898120f9992f4cb8c320c488bc3f3c8607cc06ace96be3c5`
and reserved four CPUs, 7 GiB of memory and 50 GB of free disk.

TestRunner time uses BuildBuddy's execution start and completion. Action-image
preparation, input fetch and output upload are separate clocks. Worker start
preceded the reported enqueue time in all four receipts, so queue duration is unknown. Guest readiness comes from the Sandbox wait span.
Linux-to-systemd and guest request-to-completion time use CRI console
timestamps, which include buffering. The prior production SSD reference and
this worker-disk trial use different cache and background-load windows.
Cold worker runs start with new executor disks and share the host's existing
containerDisk image, registry mirror and upstream cache. The trial kept
Cloud-direct input staging on both media. The earlier OVH proxy trial measured
Firecracker input staging with a different input tree and cache route.
[Shared CAS proxy trial](../kvm_shared_cache_trial.md) ·
[CI latency report](https://agentydragon.github.io/ducktape/latency/#kvm-cas)

| Run                                                                                         | Verdict    | Worker prep, s | Input fetch, s | TestRunner, s | Fixture setup, s | Guest-ready spans, s |
| ------------------------------------------------------------------------------------------- | ---------- | -------------: | -------------: | ------------: | ---------------: | -------------------- |
| [SSD cold cache](https://app.buildbuddy.io/invocation/db721c4d-306c-470b-a4cf-b1aa3245eba1) | 2/2 passed |          82.56 |          72.36 |        518.36 |           246.82 | 118.34 / 126.95      |
| [HDD cold cache](https://app.buildbuddy.io/invocation/2824f5fe-7609-4ad1-9434-ef70ad7693fd) | 2/2 passed |          98.22 |         159.16 |      1,457.80 |           918.51 | 202.44 / 208.52      |
| [SSD warm cache](https://app.buildbuddy.io/invocation/695cdf27-02b2-44d6-b36d-ba94913ec65b) | 2/2 passed |           0.01 |           0.30 |        344.88 |            92.10 | 110.68 / 116.47      |
| [HDD warm cache](https://app.buildbuddy.io/invocation/07c5e06f-b9fa-4a01-b01e-20e4804025f9) | 2/2 passed |           4.67 |           0.64 |        672.46 |           209.76 | 169.29 / 177.21      |

Start request to scheduler-backed Ready took 267 s on SSD and 309 s on HDD. Cold fixture setup took 246.82 and 918.51 s. The nested image-import envelope grew from 199.13 s to 876.48 s (4.40×). Both warm runs skipped all 25 import streams. The test's own spans record Sandbox startup and cleanup, with guest shutdown evidence retained in BuildBuddy.

### Image inventory and import time

The 25 images reference 2,716,738,205 stored layer bytes. Deduplicating by digest leaves 85 layers and 2,611,660,534 bytes. Of that total, 2,360,653,312 bytes are uncompressed tar, 195,577,479 bytes are OCI gzip and 55,429,743 bytes are Docker gzip. Image totals can count a shared layer more than once. Containerd streams missing blobs and creates overlay snapshots. The layer byte counts describe the OCI input and the next section measures device I/O.

| Image                             | Layer bytes | Gzip bytes | SSD cold import, s | HDD cold import, s |
| --------------------------------- | ----------: | ---------: | -----------------: | -----------------: |
| `kyverno:v1.19.1`                 |  47,761,602 | 47,761,602 |               4.61 |              24.97 |
| `kyvernopre:v1.19.1`              |  42,553,542 | 42,553,542 |               3.63 |              48.15 |
| `virt-api:v1.8.2`                 |  81,479,386 |  8,150,746 |              12.23 |              57.96 |
| `virt-controller:v1.8.2`          |  80,178,906 |  8,150,746 |               3.94 |              27.56 |
| `virt-handler:v1.8.2`             | 260,376,794 |  8,150,746 |               9.24 |              56.39 |
| `virt-launcher:v1.8.2`            | 538,430,170 |  8,150,746 |              26.46 |              59.28 |
| `virt-operator:v1.8.2`            | 133,181,146 |  8,150,746 |              11.77 |              42.51 |
| `cert-manager-cainjector:v1.21.2` |  12,391,408 | 12,391,408 |               8.13 |              49.59 |
| `cert-manager-controller:v1.21.2` |  24,888,546 | 24,888,546 |               8.88 |              14.32 |
| `cert-manager-webhook:v1.21.2`    |  19,818,817 | 19,818,817 |               9.32 |              29.34 |
| `trust-manager:v0.25.0`           |  13,952,822 | 13,952,822 |               2.06 |               8.28 |
| `agentplane-action-service`       |  79,878,718 |    856,638 |               8.72 |              63.64 |
| `agentplane-app`                  |  66,433,598 |    856,638 |               8.22 |              19.45 |
| `agentplane-egress-proxy`         |  70,416,958 |    856,638 |               6.25 |              24.67 |
| `agentplane-egress-sidecar`       |  42,328,638 |    856,638 |               4.10 |              22.70 |
| `agentplane-environments`         |  62,911,038 |    856,638 |               8.57 |              20.57 |
| `agentplane-history-service`      |  60,924,478 |    856,638 |               6.58 |              22.76 |
| `agentplane-llm-ingress`          |  57,012,798 |    856,638 |               3.12 |              21.82 |
| `agentplane-notification-service` |  68,317,758 |    856,638 |               3.35 |              24.28 |
| `agentplane-policy-controller`    |  51,329,598 |    856,638 |               2.81 |              22.55 |
| `agentplane-postgres`             |  82,606,366 | 82,606,366 |               6.66 |              18.82 |
| `agentplane-runner-disk`          | 180,940,800 |          0 |               4.57 |              22.98 |
| `agentplane-sessions`             |  62,675,518 |    856,638 |               3.69 |              36.81 |
| `agentplane-vm-guest-boot`        |  15,380,480 |          0 |               1.13 |              24.32 |
| `agentplane-vm-guest-root`        | 560,568,320 |          0 |              27.04 |             108.61 |

### Observed I/O

The table uses the earliest and latest node-exporter scrapes inside each interval, about one minute apart. The byte and busy-time deltas cover only that observed sample interval. Counters come from wyrm2's Piraeus SSD (`vdh`) and HDD (`vdi`) virtual devices and include other users of each device. I/O pressure is node-wide Linux PSI `some`. Image import is part of the TestRunner window. These counters omit underlying Atlas NVMe or RAIDZ2 device traffic.

| Run and phase                           |  Wall, s | Counter window, s | Read, GiB | Written, GiB | Disk busy | I/O PSI some |
| --------------------------------------- | -------: | ----------------: | --------: | -----------: | --------: | -----------: |
| ssd-first-boot · startup                |   267.00 |            180.00 |      0.02 |         3.59 |     16.5% |         7.6% |
| hdd-first-boot · startup                |   309.00 |            240.00 |      0.08 |         3.12 |     61.8% |         6.7% |
| hdd-cold · process                      | 1,457.80 |          1,380.00 |      3.13 |         9.55 |     93.1% |        10.5% |
| hdd-cold · nested image import envelope |   876.48 |            840.00 |      1.84 |         7.28 |     98.3% |        11.7% |
| hdd-warm-1 · process                    |   672.46 |            600.00 |      0.71 |         1.89 |     84.7% |         9.3% |
| ssd-cold · process                      |   518.36 |            480.00 |      4.33 |         8.73 |     40.6% |         4.7% |
| ssd-cold · nested image import envelope |   199.13 |            120.00 |      1.28 |         3.57 |     58.8% |         7.8% |
| ssd-warm-1 · process                    |   344.88 |            240.00 |      0.54 |         0.85 |     24.4% |         4.0% |

QEMU counted 3,077,193,728 read bytes and 18,499,571,712 written bytes on the canary's own HDD work device from boot to the warm action submission. That includes worker bootstrap, the cold action and its input staging. Across the separately bracketed warm action it counted 697,199,616 read bytes and 1,684,799,488 written bytes. The QEMU counts describe the guest work disk. The table above counts all I/O on the underlying wyrm2 virtual device. These are measurements at two layers of the same path.

### Placement and cleanup

The KubeVirt compute container requests 8,468 MiB. OVH HDD worker memory request headroom was `ovh-ns102453` 4.23 GiB, `ovh-ns103656` 4.11 GiB, `ovh-ns103711` 4.90 GiB. All three had nested KVM and fell below the matched worker's memory request, so the trial stayed on wyrm2.

Production was fully Ready in 127 of 127 samples during the monitored interval. After the final action, the canary was idle and drained. Both work claims, VMs, VolumeAttachments, runtime configuration and the trial pull secret were removed. SSD PV `pvc-6b6f6e4f-a4d8-4c1a-8aab-ab6aa626c4fa` reclaimed automatically. An ownership and detach check confirmed the HDD Retain PV `pvc-7f559530-66ea-4c3a-97b4-5897a1e46ca0` before removing its PV and LINSTOR resource. LINSTOR showed wyrm2's HDD pool free space rise from 325.21 GiB to 341.69 GiB.

### Follow-up before changing production placement

Run the required `//agentplane/system_tests/suites:test_vm` on matched SSD
and HDD workers on one host. Check `recycled-runner.json` and
`persistence-suite.json` for the same runner and a warm fixture with
complete cleanup. This trial warmed caches with the preceding action and
kept each worker running. A repeat on the same warmed work volumes after
reboot would measure the effect of losing in-memory state.
