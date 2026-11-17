# Raw block failure cleanup qualification

The real retained-stack failure probe on 17 November 2026 rejected the merged cleanup guard
as a complete release mechanism. In [the uncached RBE run](https://app.buildbuddy.io/invocation/6a3f0313-67cf-4925-a3cf-f45d48374da5),
one test body reached a Ready Sandbox with a KubeVirt VM and raw workspace PV, then injected an
error. The guard stopped its owned K3s server, captured CRI tasks and the QEMU process with the
workspace device open. No external userspace FD owner remained. The raw loop stayed attached,
and teardown replaced the injected error with an `ExceptionGroup`.

## Kernel-held block device lease

After BuildBuddy removed the disposable Docker runner, a read-only `LOOP_GET_STATUS64` inspection
on the same worker found the raw loop still backed by the probe's exact image inode. A second
loop reported `lo_rdevice = 7:1`, the raw loop's block device identity, with AUTOCLEAR disabled.
This identifies a kernel-held dependency; a `/proc/*/fd` scan cannot see it.

The pinned K3s `v1.34.12+k3s1` uses Kubernetes `v1.34.12-k3s1`. Its
[`MapBlockVolume`](https://github.com/k3s-io/kubernetes/blob/v1.34.12-k3s1/pkg/volume/util/util.go#L448)
creates a loop-backed file descriptor lock for the per-Pod block-device bind mount.
[`AttachFileDevice`](https://github.com/k3s-io/kubernetes/blob/v1.34.12-k3s1/pkg/volume/util/volumepathhandler/volume_path_handler_linux.go#L89)
uses `losetup -f` without AUTOCLEAR. Normal `UnmapBlockVolume` releases that lease. Stopping kubelet
before this reconciliation leaves the kernel mapping alive even after the processes and Docker
mount namespace disappear. The retained guard addresses process ownership; CRI removal alone does
not perform kubelet's volume unmap.

As a causal check, the probe's raw mapping and its dependent mapping were opened and checked
against their recorded kernel device/inode identities. Clearing only the verified dependent
mapping, then closing both diagnostic FDs, removed both loops. No unrelated loops were cleared
and no worker restart was needed.

The [machine-readable evidence](raw_block_kernel_lease_2026_11_17.json) records native action and
response digests, artifact checksums, the guard outcome, exact loop status, the causal cleanup,
and checksums for the matching Kubernetes and worker-kernel sources. The injected failure tests
failure cleanup; it makes no orderly guest-shutdown or performance claim.

## Acceptance boundary

Before restarting mixed latency comparisons, teardown must release dependent mappings only while
it still holds the raw volume's original FD and can verify its backing inode. Candidate discovery
and detach must use the same opened dependent FD. A reused loop number or an unrelated volume
must not authorize cleanup. A real kernel lease test must preserve unrelated volume data, and
the real Ready-VM sentinel probe must finish with both mappings gone and the original error
preserved. Production routing and resource limits remain unchanged.

## Repair qualification

The [uncached repair probe](https://app.buildbuddy.io/invocation/fec52045-b8a6-4712-b699-b97fc7469a3c)
passed one real Ready-VM injected-error body. The same kernel lease remained after the retained
guard stopped K3s and all 53 captured container task processes, then disappeared during storage
release. The original sentinel object reached the test unchanged; the raw loop's final sysfs
mapping was empty. The action explicitly requested runner disposal and exited successfully. A
post-action inventory of all four current workers found no owned diagnostic container or private
recycled-data loop.

The [focused uncached RBE target](https://app.buildbuddy.io/invocation/5a4719aa-006e-4505-8d70-05e491fc7851)
ran nine passing cases with no errors or skips, including a real dependent loop with all setup
FDs closed, unrelated-volume/data preservation, normal detach and refusal on mismatched backing
inode. These qualify failure cleanup at this boundary. The mixed-load comparison and known
managed guest boot failures remain separate.
