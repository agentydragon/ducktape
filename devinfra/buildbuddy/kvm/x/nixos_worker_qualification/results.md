# Prebuilt KVM worker qualification

## Cloudflare time source, 2026-11-22 UTC

A fresh VM on `ovh-ns1001419` booted the image published from
[`652bc7f`](https://github.com/agentydragon/ducktape/commit/652bc7fcd8fd14317d91bbfe892ab955d73427dc),
digest `sha256:21df8d193ec6d68d5b1a596b49ef18318cc26019560694ed16462aa6e64a87ff`.
With the worker policy allowing UDP/123 to `time.cloudflare.com`, the guest
reported `NTPSynchronized=yes` and selected `162.159.200.1` for that name.
The link and fallback NTP server lists were empty. The first poll measured
a +1.028 s offset and the second measured −114 µs with a 5.084 ms round trip.

The KVM smoke, Firecracker, and Agentplane VM targets all passed on worker
`af92a459-6cbe-4284-802d-3f49c0367fc5` with test-result caching disabled
and one attempt per target. [BuildBuddy qualification](https://app.buildbuddy.io/invocation/e5d038dc-005a-42b8-ba20-045c62b28aa8)

## Production transition qualification, 2026-11-17 UTC

The corrected image passed an isolated Ubuntu-to-NixOS template update with an
existing work disk, followed by KVM smoke, Firecracker and Claude/Codex VM tests.
Scaling the canary to zero during the VM action allowed the action to finish and
upload outputs before the executor stopped. All temporary resources were removed.

The image was built at `fea0c72d0bf37b466a2a73387e3b4dc2e7907e6c`:
`git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker-spike@sha256:8490616944d9817efd3a756a22ad54824639e3d615c7fd3de76deb9b053a0268`.
It includes the work-mount ordering repair from
[#17137](https://github.com/agentydragon/ducktape/pull/17137).
The [matched startup comparison](https://github.com/agentydragon/ducktape/pull/17177)
measured cached VM creation-to-Ready at 82–85 seconds versus Ubuntu 182–188
seconds, with similar test process time. Those startup measurements are separate
from this retained-disk qualification; no replacement-startup timing is claimed.

The rendered production change at `0c176a7cfe1ae38b18169486040cbab5402398ba`
was exercised in `buildbuddy-kvm-worker-transition` on `ovh-ns104952`, with an
isolated scheduler pool and work-disk name. CPU request/limit 2/4, four guest
vCPUs, 8 GiB RAM, an 80-GiB work PVC, one task slot, readiness and shutdown
deadlines matched production. The scoped ExternalSecret used the existing
registry credential store. No production resource was patched.

| Check                     | Result                                                                                                  | Evidence                                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Ubuntu control            | Registered and passed the KVM smoke target before the update                                            | [BuildBuddy](https://app.buildbuddy.io/invocation/f985dcdf-40a1-4a08-9ffb-33acdd81c1aa)       |
| Retained work disk        | VMI replaced; PVC and DataVolume UIDs retained; ext4 UUID and sentinel contents preserved               | PVC `f00f3a9c-4c24-415a-acfa-6b9f665045af`; filesystem `8caa3840-9f03-42f2-9924-9c25fab26365` |
| NixOS prerequisites       | Docker, containerd and executor active; no failed systemd units; formatter has `DefaultDependencies=no` | Guest QEMU-agent diagnostics and serial boot journal                                          |
| KVM smoke and Firecracker | Both targets passed; two Firecracker JUnit cases, no failures or skips                                  | [BuildBuddy](https://app.buildbuddy.io/invocation/f0a06541-1848-439e-b278-1fecfe129d23)       |
| VM during drain           | Claude and Codex JUnit cases passed, no failures or skips                                               | [BuildBuddy](https://app.buildbuddy.io/invocation/b67a6beb-0395-4336-ad2a-17e71e487eb2)       |

All runs disabled test-result caching and used unique qualification metadata.
Native execution records identify three new NixOS TestRunner actions on worker
`23bc5a83-af4c-4d77-9d59-6fe087f66912`, with `doNotCache=true`. The Python XML
contains four passing cases in total. The successful Go smoke target emits an
empty JUnit suite, so its case count is not included in that total.

The VM action's native process interval was 05:47:38.904–05:54:13.681 UTC.
Scale-to-zero was requested at 05:51:40.558 after observing its running action
container on the owned guest. The executor received SIGTERM and canceled
registration at 05:51:41.278. Native output upload completed at 05:54:14.893;
the guest logged successful graceful stop at 05:54:15.300, then unmounted the
work disk and powered down. The first empty VM/VMI/launcher observation was
05:54:24.341. Exact-name checks confirmed removal of the work PVC, DataVolume
and PV, pool, ConfigMap, policy, Service, ExternalSecret and pull Secret by
06:00:37. Production pool configuration was unchanged, apart from its normal
autoscaler-controlled replica count; its ConfigMap and policy also matched the
pre-change source.

The retained filesystem does not imply reusable Docker image layers. Ubuntu's
Docker 29.1.3 used the containerd image store; this image's Docker 29.9.2 uses
`overlay2`. The first NixOS action repulled its image while the old store remained
on disk. The canary still had approximately 55 GiB available during Firecracker;
fresh live samples from six reporting production workers had at least 57 GiB
available. These are point-in-time disk checks, not a capacity or RAM study.
Allow for a first-action cache warm-up and check free space during rollout.

This adds one retained-disk transition and one in-flight drain on the exact
production candidate. It does not establish fleet capacity, memory savings,
end-to-end CI gains or 24-hour drain behavior. Production rollout remains a
separate post-merge check; future image builds require their own qualification.

## Original isolated qualification

The results below describe the earlier `1ebc…` artifact, before the retained-disk
check above and the linked repeated-boot comparison. Its sample limits apply to
that original run.

The stock Linux 6.12 candidate passed all seven exercised cases: KVM/device and
container prerequisites (three), Firecracker (two), and Claude/Codex VM scenarios
(two). The VM action finished during a requested worker shutdown, uploaded its
outputs, and was followed by graceful executor shutdown and VM/PVC removal.
This qualifies one isolated worker under the conditions below, not production
capacity or a CI latency improvement.

### Tested artifact

- Source: `a3157761be8e97e336ef633e9792aa1029bbae41`.
- ContainerDisk:
  `git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker-spike@sha256:1ebc5085510946bf751a665f17379c172a97b72527514186be0b5501b7a0005a`.
- Guest: Linux 6.12.115, Docker 29.9.2, and the module's pinned BuildBuddy
  executor v2.321.0. The compressed local archive is 1,450,321,873 bytes.
- One VM on `ovh-ns104952`: 4 vCPUs, CPU request/limit 2/4, RAM 8 GiB,
  a fresh 80-GiB work PVC, and one executor task slot advertising 7 GiB.
- BuildBuddy pool `kvm-vm-nixos-spike`; execution worker
  `34932f7d-3245-448f-adc3-d818704cb07b`, matched to the guest's startup log.

All three manual targets use the existing test sources, inputs and assertions,
with only the pool overridden. Runs used `--nocache_test_results`, a unique
`KVM_WORKER_QUALIFICATION_RUN_ID`, and image-digest build metadata. The execution
records report `doNotCache=true` and the canary worker above. No cases were
filtered or skipped.

### Observed results, 2026-11-17 UTC

| Check                   | Result                                                               | Evidence / clock                                                                        |
| ----------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Fresh boot              | One boot; required modules loaded; no failed systemd units           | Guest boot ID `e720c68f-8509-4ec9-aab9-b69e02e53926`                                    |
| Offline executor start  | Local archive loaded; executor started at 32.778 s                   | systemd monotonic timestamp; runtime uses `--pull=never`                                |
| Scheduler readiness     | Healthy at 38.260 s after kernel start                               | Guest journal monotonic clock; `/readyz` returned `OK`                                  |
| Cold containerDisk pull | 62.982 s                                                             | Kubelet event; OCI image size 1,501,488,858 bytes                                       |
| VM creation to Ready    | 162 s                                                                | Kubernetes timestamps: 02:15:48 to 02:18:30                                             |
| KVM smoke               | 3 passed: KVM API 12 + VM/vCPU creation, session keyring, pivot-root | [BuildBuddy](https://app.buildbuddy.io/invocation/2bdc64f0-2407-41d4-b124-53e800bc0767) |
| Firecracker             | 2 passed; pytest elapsed 260.15 s                                    | [BuildBuddy](https://app.buildbuddy.io/invocation/2f7fc594-8cb8-44b9-84ec-d24c4d9db2d4) |
| VM during drain         | Claude and Codex passed; pytest elapsed 360.21 s                     | [BuildBuddy](https://app.buildbuddy.io/invocation/59f53eb3-3e41-4389-861c-d6533859757f) |

The canary's enforced Cilium policy allowed DNS, BuildBuddy and the internal OCI
cache, excluding Ubuntu package hosts, Nix package caches and the upstream
executor registry. Registration therefore exercised the prebuilt packages and
embedded executor archive. Test inputs and action images still transfer normally;
this is not a claim that CI performs no downloads.

### In-flight drain

At 02:30:49.564, the canary reported one action in its execution stage. A single
scale-to-zero request targeted only its pool, with a precondition of one replica.
The executor caught SIGTERM at 02:30:50.715 and cancelled scheduler registration.
It kept the action running instead of accepting new work.

BuildBuddy records execution completion at 02:34:25.281 and output upload
completion at 02:34:27.392. The guest then logged successful graceful executor
stop at 02:34:27.732, followed by Docker shutdown, `/buildbuddy` unmount, and
power-down. The VM, VMI, launcher and work PVC disappeared. Remaining temporary
ConfigMap, ExternalSecret/pull Secret, pool, policy and Service were removed.
The production Ubuntu pool, autoscaling and required test routing were unchanged.

### Constraints learned

The first image used the pinned default Linux 6.18.60, whose configuration
disabled legacy xtables. It booted and passed the three low-level checks, but
[both Firecracker cases failed in setup](https://app.buildbuddy.io/invocation/14c763bf-2d57-4cb9-9da1-b34147da5097):
K3s kube-proxy could not initialize the legacy `nat` table. The guest journal and
`modinfo` confirmed missing IPv4/IPv6 legacy table modules. The stock 6.12 LTS
kernel includes them. The prerequisite unit now strictly loads and verifies all
required modules before registration; tolerant `systemd-modules-load` alone was
insufficient. No custom kernel or test-coverage reduction was needed.

The first manifest also used a generic `work` DataVolume template. KubeVirt
appends the member index rather than the pool name, which could collide with
production `work-<index>` PVCs. The final template uses a canary-specific prefix;
the observed fresh PVC was `buildbuddy-kvm-worker-nixos-spike-work-0`.

Registry access initially stopped qualification before VM creation. The separate
[namespace allowlist PR #17092](https://github.com/agentydragon/ducktape/pull/17092)
was merged and reconciled by Flux before retrying. No live allowlist patch,
manual credential copy, or new token was used.

### Limits

There is one successful boot of the final candidate and one run per workload,
including one in-flight drain. The 24-hour shutdown allowance was not soak-tested.
The large OCI disk adds a measured cold-pull cost. There was no matched Ubuntu
control or concurrent burst, so no p50/p90 distribution, fleet-capacity conclusion
or end-to-end CI speedup is claimed. Some BuildBuddy queue timestamps were
inverted; those queue intervals remain unknown, not zero. The subsequent matched
comparison and retained-disk qualification are linked above; neither establishes
fleet capacity or end-to-end CI savings.
