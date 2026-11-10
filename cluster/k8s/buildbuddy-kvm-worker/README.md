# KVM workers

A KubeVirt `VirtualMachinePool` of one to three VMs, each containing a BuildBuddy executor for the manual
`kvm-vm` pool; BuildBuddy schedules across them, one action per VM. Required pod anti-affinity keeps
the members on different nodes without naming a node, so one node failure or its disk contention
reaches only one worker. Each VM boots KubeVirt's digest-pinned Ubuntu 24.04 cloud image, with four
vCPUs, 8 GiB RAM and an 80 GiB disk for Docker layers, action workspaces and cache.
The disk is a blank image on a filesystem volume from `piraeus-ssd-local-delete`, created per VM by a CDI
`DataVolume` template: a pool can only provide per-VM volumes that way, and CDI's importer cannot open a
raw block device (<https://github.com/agentydragon/ducktape/issues/15297>). The volume is one unreplicated
copy on the node's SSD pool; the contents rebuild. It keeps the VM on its node. A template change
replaces members one at a time; with only one member, that temporarily leaves no worker. A data-volume
template change can also replace the disk and its cache. Deleting a member deletes the volume claim and
its underlying volume. Existing volumes created with `piraeus-ssd-local` retain their
old reclaim policy and leave a volume behind when their claim is deleted; clean those up separately.
A member counts as ready once its executor has registered with BuildBuddy (the
executor's `/readyz` on port 8080, which needs the `server-type` header), so the pool restarts members one at a time
and waits for each to register before the next. Pool scale-in asks KubeVirt to shut down the
VM through ACPI; the executor stops accepting work and drains active tasks for up to the
pinned 24-hour task limit plus its 1-minute termination grace, then has 15 seconds for cleanup.
BuildBuddy applies action timeouts after input download, so a task still preparing inputs at
shutdown can be canceled before using its full action timeout. Docker, systemd and KubeVirt
have longer outer deadlines. This covers normal pool scale-in, not node or host failure. Roll
out this template update in a quiet window: existing VMs retain the old short stop deadlines
until replaced, so the update can interrupt active work. Before enabling autoscaling, verify
both VMs were replaced from the new template and re-registered.
Cloud-init installs Ubuntu's Docker package and the native
[worker configuration](../../../devinfra/buildbuddy/kvm/worker/). There is no custom guest
image build. Docker package installation uses the Ubuntu repositories at first boot.
Both Docker's data root and containerd's image store use the work disk; Docker 29's
containerd image store does not follow Docker's `data-root` setting.
Actions also bind K3s's data directory from the ext4 work disk so its nested overlayfs
snapshotter has a supported backing filesystem.

KEDA scales the pool from BuildBuddy's executor metrics in Mimir. Demand combines busy workers and
queued reservations; BuildBuddy can place more than one reservation for a task, so this is a proxy for
backlog rather than an exact task count. Scale-up requires a two-minute stabilized recommendation and
adds at most one VM per 15 minutes. It holds the requested count while a VM is booting, unregistered,
or missing metrics, and alerts if that persists. Scale-down uses a 15-minute stabilization window and
removes at most one VM every five minutes. Missing pool metrics hold the current count. Newly provisioned
workers need time to install packages and warm their local cache.
The pool manifest initializes `spec.replicas` to one because KubeVirt's scale subresource requires the
field; the worker Flux Kustomization ignores subsequent changes to it so the HPA can scale the pool.

Before enabling autoscaling, deploy the longer executor drain timeout and verify that both existing
workers have rolled out and registered with it. The initial rollout can interrupt a busy worker still
using the old short timeout; schedule it for a quiet window. Graceful drain is bounded and does not
guarantee that every possible action completes before shutdown.
The guest sets kubelet's kernel defaults at boot. The KVM fixture prepares its private
Docker cgroup and network namespaces; guest-wide sysctls remain read-only in the action.
K3s pulls its bundled images through Zot, with public registry fallback disabled, using
read-only configuration supplied by the worker.

The existing `buildbuddy-api-key` Secret is attached as a read-only ISO. No outer Kubernetes
token or host filesystem is attached. Root in a test can compromise the persistent guest
and its BuildBuddy key; this is a trusted-code worker.
Actions receive all guest Linux capabilities: nested runc needs `SYS_RESOURCE` to set
pod OOM scores, and KubeVirt's privileged node agents need the full capability set.
Docker packages the action's filesystem; the VM provides isolation from the outer cluster.
Nested runc creates pod session keyrings and adjusts their permissions. The guest's Docker
seccomp profile allows the `KEYCTL_JOIN_SESSION_KEYRING`, `KEYCTL_DESCRIBE` and
`KEYCTL_SETPERM` operations; other `keyctl` operations remain blocked. It is based on Docker
29.1.3's [default profile](https://github.com/moby/moby/blob/docker-v29.1.3/vendor/github.com/moby/profiles/seccomp/default.json).
When the Docker package is upgraded, compare its default profile and update our copy so
other seccomp policy changes are retained. The profile applies to every container in this
dedicated guest. `pivot_root` is additionally allowed only with `CAP_SYS_ADMIN`, so
nested runc can use its normal root switch instead of the no-pivot fallback.

The Kustomization is enabled, so Flux deploys the pool when this configuration lands.
First boot configures the dedicated guest for nested KVM and reboots once before the
executor starts. The VM selects nodes whose KubeVirt CPU feature labels advertise either
`svm` or `vmx`; its guest loads the corresponding KVM driver. The worker may run on any node,
including control-plane nodes, that advertises either CPU feature and has capacity for
the 4-CPU, 8-GiB reservation. It is limited to the `hil` region, where the SSD pools are, and has no hostname or explicit OS selector. The executor advertises four CPUs and 7 GiB, leaving
1 GiB for the guest OS and Docker. KVM test actions request four CPUs, 6 GB of memory and 20 GB of free disk. The
`kvm-vm` pool name is independent of the node placement.
The labels describe hypervisor CPU features, and the guest bootstrap fails if no KVM device
is available; verify nested execution after moving the VM.
Verify guest boot, the outer network fence and these manual checks:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_vm_test --nocache_test_results
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```

The guest's `prometheus-node-exporter` exposes CPU, memory and filesystem metrics alongside the executor's own metrics.
The outer policy admits only node-local Alloy to those metrics ports, and egress to DNS, BuildBuddy and its registry, Ubuntu package
repositories over TLS, and Zot. Use
`virtctl -n buildbuddy-executor console buildbuddy-kvm-worker-0` for diagnostics. Rotate the key too if
the guest was compromised.
