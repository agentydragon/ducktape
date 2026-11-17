# Prebuilt KVM worker qualification

The stock Linux 6.12 candidate passed all seven exercised cases: KVM/device and
container prerequisites (three), Firecracker (two), and Claude/Codex VM scenarios
(two). The VM action finished during a requested worker shutdown, uploaded its
outputs, and was followed by graceful executor shutdown and VM/PVC removal.
This qualifies one isolated worker under the conditions below, not production
capacity or a CI latency improvement.

## Tested artifact

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

## Observed results, 2026-11-17 UTC

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

## In-flight drain

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

## Constraints learned

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

## Limits

There is one successful boot of the final candidate and one run per workload,
including one in-flight drain. The 24-hour shutdown allowance was not soak-tested.
The large OCI disk adds a measured cold-pull cost. There was no matched Ubuntu
control or concurrent burst, so no p50/p90 distribution, fleet-capacity conclusion
or end-to-end CI speedup is claimed. Some BuildBuddy queue timestamps were
inverted; those queue intervals remain unknown, not zero. Production adoption
still needs a matched capacity/latency experiment on the candidate digest.
