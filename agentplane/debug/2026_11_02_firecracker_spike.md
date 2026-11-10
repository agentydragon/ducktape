# Firecracker environment primitive proof

> Historical spike: its independent test flock is not Environment Runtime’s current COSI database lock.

The standalone spike runs Firecracker in a Kubernetes Pod on the KVM-capable `wyrm2` node. It boots
the real Agentplane runner disk alongside separate state, workspace, config, trust and credential
disks, and resumes a synthetic workload from a RAM snapshot after the original VMM exits.
The real egress relay handles guest proxy traffic while pod-local filtering rejects bypasses.

The reproducible harness is [`x/firecracker_spike`](../x/firecracker_spike/README.md). The
[structured results](firecracker_spike_2026_11_02/results.json),
[boot log](firecracker_spike_2026_11_02/boot.log),
[restore log](firecracker_spike_2026_11_02/restore.log), and
[input hashes](firecracker_spike_2026_11_02/input_hashes.txt) are the evidence. RAM and disk images
are deliberately excluded from Git. No Agentplane backend, Sandbox, production policy, or Flux
configuration changes are part of this spike.

## Setup and prerequisites

The source baseline was `e5a5856517` on `origin/devel`. The RBE-built
`//agentplane/x/firecracker_spike:agentplane_inputs` packages the existing
`//agentplane/env/images:disk_erofs` and `//agentplane/egress:sidecar_go_binary_layer` targets.
The initial artifact build passed in
[BuildBuddy](https://app.buildbuddy.io/invocation/50ecff8c-72b5-4335-b1c9-c0fa3c2244b1).
The final artifact and probe-library build, including Ruff and mypy aspects, passed in
[BuildBuddy](https://app.buildbuddy.io/invocation/8c71fc88-5d4c-486f-a48d-f5bf51668fdf).
The live acceptance run returned `status: passed`; console logs are normalized to LF for storage.

The live run uses an x86-64 Ubuntu 24.04 launcher Pod, Firecracker `v1.17.0`, a locally compiled
Linux `6.1.188` ELF kernel, and the official Firecracker CI Ubuntu 24.04 root filesystem.
The guest has one vCPU and 256 MiB RAM. The launcher requests two CPUs and 2 GiB memory, with limits
of four CPUs and 4 GiB, and requests one `devices.kubevirt.io/kvm` device. It is privileged and
mounts `/dev/net/tun`; it has no host network, host PID namespace, general host filesystem mount, or
ServiceAccount token. The outer host runs Linux `6.18.55` on an AMD Ryzen 9 9950X3D, with
containerd `2.3.3` and Kubernetes `1.36.3`.

This is a native kubelet worker of the existing cluster, **not k3s**. k3s itself does not require
KVM, but Firecracker does. The existing RBE k3s fixture runs without KVM; its cgroup cleanup must
not be reused against a live host's cgroup tree. The optional additional k3s experiment was left
out after clarification with the operator. These results establish the Kubernetes Pod/KVM
boundary on this host and CNI; they do not certify a k3s/Flannel deployment.

## What Agentplane currently requires

| Boundary             | Current contract                                                                                   | Spike treatment                                                                                             |
| -------------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Runtime distribution | Independently released read-only EROFS at `/run/agentplane/environment`                            | Mount the actual runner disk; execute Runtime `--help` and verify the execution companion's namespace guard |
| State                | `/state`, currently virtiofs for VMs; journals, native continuation files and owner lock live here | Dedicated ext4 virtio block device; exercise SQLite and an exclusive `flock`                                |
| Workspace            | Separate ext4 block filesystem at `/state/workspaces`                                              | Dedicated writable virtio block device nested under state                                                   |
| Public boot inputs   | ISO9660 config, launch and egress CA disks                                                         | Separate read-only ISO9660 drives at the existing guest paths, with synthetic configuration                 |
| Runtime identity     | Root-only TLS Secret and separate public trust, currently virtiofs                                 | Synthetic root-only credential ext4 disk and trust path; no credential provisioning or rotation integration |
| Outbound traffic     | Launcher relay holds projected tokens; guest uses proxy and platform-route listeners               | Actual relay binary with synthetic tokens and a controlled upstream tunnel                                  |
| Inbound control      | Gateway reaches guest port `9090` using mTLS, identity and connection-epoch fences                 | Launcher reaches the synthetic guest HTTP listener through TAP; actual Gateway control remains untested     |
| Workload supervision | Production VM Runtime uses systemd/D-Bus and environment cgroups                                   | Bash PID 1 plus an observable Python workload and child; actual Runtime supervision remains untested        |

The source contracts are [the disk contract](../env/images/vm_guest_contract.json),
[VM storage/networking](../docs/vm_sandboxes.md), [retained state](../env/runtime/docs/retained_state.md),
[Runtime control](../env/runtime/docs/control.md), and
[relay configuration](../../cluster/cdk8s/agentplane/launcher_relay.py).

## Observed acceptance results

| Experiment               | Required observation                                                                                                                | Result                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Build and hello world    | Custom kernel boots and the guest serves its readiness/state endpoint                                                               | Passed                                                 |
| Separate disks           | Eight virtio block drives reach the intended mount points                                                                           | Passed                                                 |
| Runner update boundary   | The runner executable comes from the independent source-built EROFS disk; writes fail                                               | Passed                                                 |
| Config and credentials   | Public inputs are readable; config writes fail; uid 107 cannot read the root-only dummy key                                         | Passed                                                 |
| State ownership          | A second process cannot acquire the same state-owner `flock`                                                                        | Passed                                                 |
| Resident freeze/thaw     | API reports `Paused`, guest stops answering, then process identity/RAM/file offset continue                                         | Passed                                                 |
| RAM checkpoint           | Full snapshot saves 268,435,456 bytes of memory and VMM device/CPU state                                                            | Passed                                                 |
| Restore after VMM death  | Kill the original Firecracker process; a new process restores the same guest workload                                               | Passed twice, sequentially                             |
| Open resources           | Restored PID, child PID, RAM checksum, counter, boot ID, file inode and offset match; the already-open file reads its unread suffix | Passed                                                 |
| SQLite                   | Rollback-journal database remains valid after restore; cold restart adds a row                                                      | Passed                                                 |
| Cold restart             | New RAM nonce/boot ID, retained state and workspace sentinels                                                                       | Passed                                                 |
| Proxy connectivity       | HTTPS succeeds through the real relay over guest IPv4 and IPv6                                                                      | Passed                                                 |
| Relay identity mechanics | Forged proxy/audience headers are replaced; changing the hop-token file works without relay restart                                 | Passed                                                 |
| Network fence            | Direct public HTTPS, direct upstream bypass, another IPv6 host port and UDP bypass fail                                             | Passed; positive controls and reject counters recorded |
| Proxy outage             | Stopped relay fails proxy requests; direct public access remains blocked                                                            | Passed                                                 |
| Post-restore network     | New proxy connections work and direct public access remains blocked after each restore                                              | Passed                                                 |

The guest retains an unpredictable nonce and the checksum of 8 MiB of random bytes generated only
at process startup. The checkpoint counter is advanced before capture and again after a restore;
restoring the saved disk/RAM pair returns it to its checkpoint value. The open workspace descriptor
starts at offset six in `hello world\n`; each restore reads `world\n` from that descriptor.
The guest's boot ID remains constant across RAM restore and changes on cold boot.

The launcher copies **all eight backing disks while the guest is paused**, alongside the RAM and
VMM state, and terminates each continuation before starting another. Before the second restore,
it reinstates that saved disk set. This avoids pairing old RAM with disk writes from a later
continuation. The backing images are ordinary files in an `emptyDir`; raw block PVC attachment,
PVC snapshots, Pod replacement, storage failures and cross-node restore were not exercised.

## Findings that affect a backend design

1. **The stock guest kernel cannot mount the current runner/config formats.** The pinned upstream
   Firecracker kernel configuration disables EROFS and ISO9660. Enabling them built-in allowed the
   existing runner disk and separate config disks to mount without repackaging their filesystems.
   The guest also needs a virtio-mmio boot path; the production KubeVirt guest image was not booted.
2. **Drive API identity and guest mount identity differ.** Firecracker rejected
   `agentplane-runner` as an API resource ID; the harness uses underscores. More fundamentally,
   Firecracker's [virtio block implementation](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/devices/virtio/block/virtio/device.rs#L135)
   derives its block identifier from backing-file metadata. The current guest's KubeVirt serial
   selectors need an explicit replacement, such as controlled filesystem labels/UUIDs. This spike
   uses the known boot attachment order `/dev/vda` through `/dev/vdh`; it does not claim stable
   production discovery across disk changes.
3. **State and credential delivery need a new mechanism.** Firecracker does not supply the current
   KubeVirt virtiofs attachments. ext4 block-backed state and private credential delivery work as
   primitives, but this changes the Pod/VM shared filesystem contract. A credential disk is a
   snapshot of its contents, not a rotating Kubernetes Secret mount.
4. **TAP MTU must fit the Pod's path.** With a 1500-byte guest interface and a 1370-byte Pod
   interface, direct HTTPS timed out before the firewall existed. The
   [packet capture](firecracker_spike_2026_11_02/mtu_packets.txt) shows a completed TCP handshake,
   followed by receipt of a later TLS segment and repeated acknowledgement of a gap. After setting
   guest and TAP MTUs to 1280, the same direct-egress control succeeded. This isolates an MTU-related
   failure here; a production implementation must derive an appropriate MTU from the CNI path.
5. **IPv6 filtering must retain neighbor discovery.** A blanket TAP input rejection also broke
   IPv6 proxy access. Allowing neighbor solicitation/advertisement with hop limit 255 restored it;
   other guest traffic remained rejected. IPv6 proxy success here uses an IPv6 guest-to-relay hop
   and an IPv4-capable upstream; public IPv6 egress was not tested.
6. **RAM restore rolls back guest identity and clocks.** Both restores retain the same boot ID,
   owner lock and process memory. The recorded second continuation's guest clock can precede the
   first continuation's observation. No wall-clock repair, entropy protocol, certificate refresh,
   or established TCP/gRPC connection preservation is claimed. Upstream also warns that existing
   [network connections may not survive restore](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/snapshot-support.md).

## Remaining Agentplane acceptance work

A backend needs a single owner for launcher creation, shutdown, reconciliation and cleanup, with
an external exclusive-writer fence covering every disk/RAM continuation. An in-guest `flock`
cannot fence two clones of the same VM memory. The experiment deliberately runs continuations
sequentially and does not replay any real Agentplane operation.

The resume contract must decide how a preserved Runtime process re-enters the current
incarnation, boot-ID and connection-epoch rules. Current VM stop/start creates fresh launch and
credential identities; RAM restore does not execute those startup paths. Credential renewal,
clock repair, reconnects and observation of work whose external effects are uncertain need an
explicit thaw/reconciliation protocol before restoring real sessions.

The launcher also needs the equivalent of KubeVirt's UID-checked admission and token isolation,
Gateway-only mTLS ingress, central egress authorization and Cilium enforcement. The synthetic
upstream checks sidecar header replacement and token-file refresh; it does not TokenReview a
real ServiceAccount, enforce production destination policy, terminate TLS for credential
substitution, or exercise provider requests.

Production qualification remains separate: jailer/seccomp/cgroup isolation, a least-privileged
device/network setup, NixOS/systemd Runtime supervision, harness/tool compatibility, retained-PVC
ownership, crash consistency under storage failure, CPU/snapshot compatibility, Pod/node loss,
and snapshot protection/retention. No startup or restore performance claim is derived from this
functional experiment.
