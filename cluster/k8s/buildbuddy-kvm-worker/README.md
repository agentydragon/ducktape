# KVM workers

A KubeVirt `VirtualMachinePool` of two VMs, each containing a BuildBuddy executor for the manual
`kvm-vm` pool; BuildBuddy schedules across them, one action per VM. Required pod anti-affinity keeps
the members on different nodes without naming a node, so one node failure or its disk contention
reaches only one worker. Each VM boots KubeVirt's digest-pinned Ubuntu 24.04 cloud image, with four
vCPUs, 16 GiB RAM and an 80 GiB disk for Docker layers, action workspaces and cache.
The disk is a blank image on a filesystem volume from `piraeus-ssd-local`, created per VM by a CDI
`DataVolume` template: a pool can only provide per-VM volumes that way, and CDI's importer cannot open a
raw block device (<https://github.com/agentydragon/ducktape/issues/15297>). The volume is one unreplicated
copy on the node's SSD pool; the contents rebuild. It keeps the VM on its node, and a template change
replaces the VM and its volume. The class retains deleted volumes, so remove a leftover volume by hand when
a worker is deleted. The pool replaces one VM at a time, so a template change never leaves the pool
without an executor.
Cloud-init installs Ubuntu's Docker package and the native
[worker configuration](../../../devinfra/buildbuddy/kvm/worker/). There is no custom guest
image build. Docker package installation uses the Ubuntu repositories at first boot.
Both Docker's data root and containerd's image store use the work disk; Docker 29's
containerd image store does not follow Docker's `data-root` setting.
Actions also bind K3s's data directory from the ext4 work disk so its nested overlayfs
snapshotter has a supported backing filesystem.
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
the 4-CPU, 16-GiB reservation. It is limited to the `hil` region, where the SSD pools are, and has no hostname or explicit OS selector. The executor advertises four CPUs and 14 GiB, leaving
2 GiB for the guest OS and Docker. KVM test actions request four compute units. The
`kvm-vm` pool name is independent of the node placement.
The labels describe hypervisor CPU features, and the guest bootstrap fails if no KVM device
is available; verify nested execution after moving the VM.
Verify guest boot, the outer network fence and these manual checks:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_vm_test --nocache_test_results
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```

The outer policy admits only node-local Alloy to the executor's metrics port, and egress to DNS, BuildBuddy and its registry, Ubuntu package
repositories over TLS, and Zot. Use
`virtctl -n buildbuddy-executor console buildbuddy-kvm-worker-0` for diagnostics. Rotate the key too if
the guest was compromised.
