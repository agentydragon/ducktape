# KVM workers

A KubeVirt `VirtualMachinePool` runs one BuildBuddy executor per VM in pool
`kvm-vm`, with one action slot per executor. KEDA permits two to eight members.
Each guest has four vCPUs and 8 GiB RAM. Its launcher requests two CPUs and can
use four. Members require nested KVM and a node-local SSD work volume.
They may share a host. Required VM-test routing is configured separately.

The pool boots a manually pinned, prebuilt [NixOS containerDisk](../../../devinfra/buildbuddy/kvm/x/nixos_worker/README.md).
Docker, containerd, node exporter, nested-KVM setup and the executor archive are
built into the image. Startup installs no packages, fetches no executor image,
and needs no configuration reboot. The initial production pin is the corrected
artifact built at `fea0c72d0bf37b466a2a73387e3b4dc2e7907e6c`, including the
work-mount ordering repair. Its historical package/repository name still
contains `spike`. The image workflow builds and publishes future candidates;
promotion requires fresh-boot and real workload qualification before a manual
digest update. A newer checkout can build a different image because flake
inputs and shared worker files also affect the derivation.

KubeVirt pulls the private containerDisk using `forgejo-images-creds`. This
namespace's Flux-owned ExternalSecret reads the existing scoped
`kubernetes-forgejo-images-secret-store`; no registry credential is copied into
the guest. The existing `buildbuddy-api-key` Secret and runtime ConfigMap are
attached as read-only ISO disks. The ConfigMap supplies `executor.yaml`,
`k3s.yaml`, `registries.yaml`, and an explicit `pool` file. The guest waits for
the disks, verifies its runtime inputs, and uses its SMBIOS UUID as its executor
hostname. SSH is disabled. Guest journals reach the existing KubeVirt console
log collector; do not type credentials into the root serial console.

Each member gets an 80 GiB work disk for Docker/containerd stores, action
workspaces, file cache and nested K3s data. CDI creates its blank image on a
filesystem PVC from `piraeus-ssd-local-delete`. It is a single local copy whose
contents are regenerable, and it keeps the member on that node. The guest
formats a blank disk once, preserves existing ext4, and rejects another
filesystem. Raw block awaits containerd device ownership support
([#15297](https://github.com/agentydragon/ducktape/issues/15297)). Deleting a
member deletes its claim and underlying volume; a template change may discard
caches. Older volumes using `piraeus-ssd-local` keep their former reclaim
policy and need separate cleanup if orphaned.

On wyrm2, the same VM TestRunner took 2.81× as long on a cold HDD work disk
and 1.95× as long with warm worker caches. The four runs each created a fresh
K3s fixture. [Placement and work-disk measurements](../../../devinfra/buildbuddy/debug/kvm_pool_rollout/2026_11_21_kvm_worker_placement.md)

The OVH shared BuildBuddy CAS proxy trial measured Firecracker input staging
and kept the current Cloud-direct route. The wyrm2 work-disk comparison held
that route fixed and measured nested K3s setup and Agentplane VM execution.
[Shared cache proxy trial](../../../devinfra/buildbuddy/debug/kvm_shared_cache_trial.md) ·
[CI latency report](https://agentydragon.github.io/ducktape/latency/#kvm-cas)

The [proposed VM fixture architecture and cost options](https://github.com/agentydragon/ducktape/pull/18694) track alternatives for future trials.

The qualified Ubuntu-to-NixOS transition retained the ext4 filesystem and work
PVC. Docker changed from Ubuntu's containerd image store to `overlay2`, so its
first action repulled the action image while the old store remained on disk.
Allow for that cache warm-up and check available work-disk space during rollout;
do not assume retained Docker layers are reusable. The
[transition and drain qualification](../../../devinfra/buildbuddy/kvm/x/nixos_worker_qualification/results.md)
records the observed workload and disk checks.

A template change replaces at most one member at a time and waits for the
replacement executor to register. Readiness uses the executor's `/readyz` on
port 8080, with the `server-type: buildbuddy-executor` header. Check active
work before merging an image change: the long shutdown grace can delay
replacement while an action drains. Use a quiet window for the initial rollout
and verify each member's image, boot journal, registration and physical
workload execution.

Normal pool scale-in sends ACPI shutdown. The executor stops accepting work
and drains active actions for the pinned 24-hour task limit plus one minute
termination grace and 15 seconds cleanup. Docker, systemd and KubeVirt have
longer outer deadlines. Input download precedes BuildBuddy's action timeout,
so a task preparing inputs can be canceled before using its full task timeout.
This bounded drain does not cover node failure or guarantee every action
finishes before shutdown.

KEDA reads fresh, registered-executor metrics from Mimir. Demand combines busy
workers and queued reservations, so duplicate reservations make it a proxy,
not a distinct task count ([#15568](https://github.com/agentydragon/ducktape/issues/15568)).
Scale-up has no stabilization delay and adds at most four members per minute.
Scale-down stabilizes for 15 minutes and removes at most one per five minutes.
While a requested member is booting, unregistered or missing metrics, KEDA holds
the requested size and alerts if the condition persists. Missing pool metrics
also hold the current count. Flux initializes `spec.replicas` to KEDA's minimum
of two for the KubeVirt scale subresource, then ignores HPA changes to that field.

The executor advertises four CPUs and 7 GiB, leaving 1 GiB for the guest. Each
KVM suite declares its scheduling estimates. The required Agentplane VM suite
uses four CPUs, 7 GiB of memory and 50 GB of free disk. These allocations and
the one-task limit stay independent of the image pin. The
[worker comparison](https://agentydragon.github.io/ducktape/latency/#worker-image-comparison)
measured faster cached startup without demonstrating faster tests, RAM savings
or additional safe replicas.

Actions are trusted code: root in a test can control the persistent guest and
its BuildBuddy key. No outer Kubernetes token or host filesystem is attached.
Actions receive the guest's Linux capabilities and `/dev`; nested runc needs
keyring operations and `CAP_SYS_ADMIN` for its normal root switch. The shared
Docker seccomp profile enables the required `keyctl` calls and conditional
`pivot_root`; review it when the guest Docker version changes. The VM provides
isolation from the outer cluster, not between hostile jobs.

K3s images use Zot with public fallback disabled. The guest's outer network
policy permits DNS, the BuildBuddy scheduler/cache and Zot. Metrics on ports
9090 and 9100 admit only node-local Alloy. Ubuntu package hosts and the executor
registry are unnecessary for guest startup. Diagnose a member with
`virtctl -n buildbuddy-executor console buildbuddy-kvm-worker-0`; inspect mount,
Docker and executor units and the boot journal for discarded ordering-cycle
jobs, not only `systemctl --failed`.

After rollout, run fresh manual workloads and verify their native worker:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_vm_test --nocache_test_results
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```
