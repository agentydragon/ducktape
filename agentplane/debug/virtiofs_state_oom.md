# virtiofs state backend OOM

**Status, 2026-10-27 PDT:** investigation active. The daemon-budget hypothesis has been checked
against the deployed configuration and the release's upstream source. No reproduction, memory
sizing change, cache-policy change or recovery action has been run. Incident:
[#12203](https://github.com/agentydragon/ducktape/issues/12203).

## Findings

There is no virtiofsd option to advertise a byte memory budget in the inspected `1.13.0` and
`1.13.3` sources. Neither reads `memory.max` or implements a cgroup-aware inode-cache budget.
KubeVirt's `80M` limit is a host-side container budget, independent of the VM's 8 GiB guest RAM
and the Runner's guest cgroups. Raising guest RAM cannot raise this daemon's limit.

The existing evidence establishes an OOM and a lost storage backend. It does not establish a
memory leak, identify the charged memory, or establish a safe replacement limit. Keep the live
VM unchanged while collecting a controlled reproduction. A resource override is available, but
choosing a value without measurements would only move the failure threshold.

## Deployed evidence

Read on 2026-10-27 using the investigation ServiceAccount's authorized Pod, Pod-log and KubeVirt
reads; no guest/container exec or node access was used.

| Item                               | Observed value                                                            |
| ---------------------------------- | ------------------------------------------------------------------------- |
| VM                                 | `agentplane-staging/public-coder-ducktape-1`                              |
| Launcher                           | `virt-launcher-public-coder-ducktape-1-kwls2`                             |
| Pod UID                            | `82f4571f-6a68-4839-9f27-088cdbf26af9`                                    |
| Node                               | `ovh-ns102453`                                                            |
| KubeVirt observed/operator version | `v1.8.2`                                                                  |
| Daemon container/image             | `virtiofs-state`, `quay.io/kubevirt/virt-launcher:v1.8.2`                 |
| Runtime image digest               | `sha256:574fabe0ff9719fa1e3800dcca8530fa4e02fd71ef74109508e59800e13e8b5d` |
| Memory request/limit               | `1M` / `80M` (80,000,000 bytes, about 76.3 MiB)                           |
| CPU request/limit                  | `10m` / `100m`                                                            |
| Identity                           | UID/GID `107:107`, all capabilities dropped                               |
| Termination                        | `OOMKilled`, exit `137`, `2026-10-27T07:26:02Z`                           |
| Restart                            | Pod `restartPolicy: Never`; container `restartCount: 0`                   |

The container runs `/usr/libexec/virtiofsd` with:

```text
--socket-path=/var/run/kubevirt/virtiofs-containers/state.sock
--shared-dir=/state
--sandbox=none
--cache=auto
--migration-on-error=guest-error
--migration-mode=find-paths
```

Its logs contain only socket-listener startup at `06:23:06Z` and client connection at `06:23:08Z`.
At collection, `compute` remained running and ready, while `virtiofs-state` was terminated and
unready. The Pod API reports configured resources and the OOM termination; the daemon's actual
cgroup files, kernel OOM record, peak RSS, descriptor count and `memory.stat` were not captured.

The incident evidence in #12203 places a copy of a roughly 169 MiB Bazel runfiles tree into
`/state/tmp` about 20 seconds before the OOM. Cross-device hardlink errors were reported and no
successful command completion was recorded. That timing suggests a workload to reproduce; it
cannot distinguish data-page charges, kernel metadata, daemon allocations or unrelated growth.
The manifest's 9,987 lines are not a measured count of distinct retained inodes.

## Where the limit and arguments come from

KubeVirt `v1.8.2`'s
[`pkg/virtiofs/resources.go`](https://github.com/kubevirt/kubevirt/blob/v1.8.2/pkg/virtiofs/resources.go)
sets `1M` / `80M` as the default memory request/limit. Its
[`pkg/virt-controller/services/virtiofs.go`](https://github.com/kubevirt/kubevirt/blob/v1.8.2/pkg/virt-controller/services/virtiofs.go)
creates one daemon container per attached filesystem and hardcodes the arguments above. The
resources are neither derived from guest RAM nor apportioned across filesystems.

The supported resource override is
`KubeVirt.spec.configuration.supportContainerResources[]`, with `type: virtiofs` and
`resources.requests.memory` / `resources.limits.memory`. It applies cluster-wide to newly generated
virtiofs containers, not to a named filesystem or an existing launcher. The live KubeVirt CR has
no override. Its declarative owner is
[`cluster/cdk8s/kubevirt/app.py`](../../cluster/cdk8s/kubevirt/app.py), then the generated
`cluster/generated/kubevirt/app/` manifests. Agentplane's
[`sandbox_controller/vm.go`](../sandbox_controller/vm.go) adds filesystem attachments; it does not
choose the daemon resources or arguments. No admission policy currently owns those settings.

`FilesystemVirtiofs` is an empty API type in this release: neither the VM spec nor the KubeVirt CR
exposes daemon cache, descriptor or thread options. Upstream
[#19337](https://github.com/kubevirt/kubevirt/issues/19337) requests those controls. Changing them
would require a maintained image/controller change or admission mutation, with its own owner
and tests; a new field in our VM template would have no upstream reader.

## Daemon controls and version limits

KubeVirt's release
[`.bazelrc`](https://github.com/kubevirt/kubevirt/blob/v1.8.2/.bazelrc) defaults to CentOS Stream 9;
its [`rpm/BUILD.bazel`](https://github.com/kubevirt/kubevirt/blob/v1.8.2/rpm/BUILD.bazel) pins
`virtiofsd-1.13.0-1.el9`. The alternative Stream 10 build pins `1.13.3-1.el10`. The running
binary's version/RPM was not read, so `1.13.0` is a source-build inference, not a runtime check.
The inspected published source archives are
[`1.13.0`](https://static.crates.io/crates/virtiofsd/virtiofsd-1.13.0.crate) and
[`1.13.3`](https://static.crates.io/crates/virtiofsd/virtiofsd-1.13.3.crate).

| Control                | What it controls; why it is not a byte budget                                                                                                                                                                                                       |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--cache=auto`         | Guest FUSE data caching and one-second entry/attribute validity. Expiry does not itself evict every server inode reference.                                                                                                                         |
| `--cache=never`        | Zero entry/attribute validity, disables `READDIRPLUS`, and requests direct guest file I/O. It can change reference retention and traffic, but imposes no server memory ceiling and disables ordinary file `mmap` unless separately allowed.         |
| `--no-readdirplus`     | Avoids combined directory enumeration and inode lookup. It does not cap explicit lookups or retained inode count.                                                                                                                                   |
| `--rlimit-nofile`      | Bounds descriptors, with a default target of up to 1,000,000 subject to inherited/kernel limits. It does not bound all allocations or cgroup page-cache charges. Lowering it can turn successful file operations into descriptor-exhaustion errors. |
| `--inode-file-handles` | Uses file handles instead of retained `O_PATH` descriptors. Reopening handles requires `CAP_DAC_READ_SEARCH`, absent from this launcher. `1.13.0` defaults to `never`; `1.13.3` defaults to `prefer`.                                               |
| `--thread-pool-size`   | Concurrency; default `0` disables the pool. Increasing it is not a memory mitigation.                                                                                                                                                               |
| Virtqueue size         | KubeVirt's converter sets `1024`; it is not a daemon byte budget.                                                                                                                                                                                   |

The source's `passthrough/inode_store.rs` tracks references and removes entries when their count
reaches zero; `FORGET`/`BATCH_FORGET` from the guest decrement references. A guest retaining
inodes can therefore retain daemon metadata and host descriptors. This is a plausible mechanism,
not a measurement of this OOM. `1.13.3` additionally reserves internal descriptors and accounts
guest descriptors (`guest_fd_limit.rs`); that protects descriptor availability, not total memory.

A separate host has a `cache=never` workaround in `cluster/terraform/main/proxmox-vms.tf`. Its
reported growth under a different VM/backend is not evidence that changing this KubeVirt guest's
cache policy prevents the incident.

## Failure and recovery semantics

A dead daemon leaves the guest's `/state` transport unavailable. The observed `Never` launcher
policy does not restart it. Guest RAM and `compute` liveness are insufficient storage-health
signals. Agentplane's controller separately gates `WorkloadReady` on launcher Pod readiness;
its `Running` condition can remain true for a running VMI with an unready launcher.

Upstream [#19431](https://github.com/kubevirt/kubevirt/issues/19431) reports the same release,
image digest and Running/unready/no-restart behavior with a different backing filesystem. It is
closed by its reporter without a linked fix or maintainer recovery guarantee. It corroborates
the symptom, not this incident's allocation mechanism.

Increasing resources for future launchers cannot revive this daemon. Restart/recreation of the
VM incarnation would lose guest RAM and needs a separate authorized recovery procedure that
retains the state/workspace PVCs. A daemon-only restart is not established as safe: the guest and
QEMU retain references into its lost FUSE state. No recovery, PVC deletion or live patch was
performed in this investigation.

## Next bounded experiment

Use the existing isolated [RBE k3s VM fixture](../system_tests/deployment/README.md#vm-sandboxes), with
production's pinned KubeVirt launcher and a disposable filesystem state claim. This shell has
neither `/dev/kvm` nor `/dev/fuse`; do not reproduce on the affected VM. The RBE fixture supports
TCG guests. Confirm that the KubeVirt-generated daemon retains its memory limit even though the
fixture's own workload manifests omit container limits. The current fixture uses local-path
storage, so a pass does not qualify the deployed Piraeus-backed state PVC.

1. Record the binary's `--version`/`--help`, image digest, host/guest kernels, real cgroup path,
   `memory.max`, `memory.high`, `memory.events`, `memory.peak`, process limits and daemon arguments.
   Keep guest RAM fixed. Write diagnostic artifacts outside the state share under the test's
   undeclared outputs directory.
2. At a one-second cadence collect `memory.current`, `memory.stat` (`anon`, `file`, `shmem`,
   `kernel`, `slab_reclaimable`, `slab_unreclaimable` where available), `memory.events`,
   `/proc/<pid>/smaps_rollup`, descriptor count and Pod/container state. Observe the daemon from
   outside its cgroup so OOM cannot kill the recorder. Capture kernel OOM evidence where permitted.
3. In separate fresh VM incarnations, run capped workloads: enumerate/stat 10,000 empty files;
   copy 10,000 files totaling at most 170 MiB from another filesystem into state; then attempt the
   incident's cross-filesystem `cp -al` and record its fallback behavior and exit status. Use a
   fixed five-minute workload deadline and collect a 60-second idle window after completion.
   Stop that run at daemon termination or 70 MB charged memory; the production-limit run may
   still overshoot between samples, but all claims and processes are disposable.
4. Start with the deployed `80M`/`auto` settings. If 70 MB is reached, repeat that exact workload
   with a fixture-only `256Mi` limit to observe whether memory plateaus, stopping at 224 MiB.
   Separately compare `never` and `auto` plus `--no-readdirplus` under the same `80M` limit,
   preserving UID/GID/capabilities. Record any `mmap` or I/O error, not just daemon survival.
5. Distinguish anonymous RSS growth from file-cache/kernel charges and descriptor/inode retention.
   If metadata grows, instrument guest `FORGET` traffic with a real tracing tool in the fixture;
   do not infer missing `FORGET` from a one-second timeout. Choose any resource increase from
   measured peak and workload headroom. Check readiness, a new successful state read/write and
   Runner progress after the workload; a running `compute` container is not acceptance.

A declarative candidate mitigation should follow that result: a measured
`supportContainerResources` override if the workload fits a bounded larger budget, or a tested
cache/descriptor change if it prevents the measured retention with acceptable guest semantics.
Require a production-backend disposable trial before claiming the reported failure is fixed.
