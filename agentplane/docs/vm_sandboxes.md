# VM Sandboxes

A `SandboxTemplate` with `vm` runs its Sandbox's runner in a NixOS guest under KubeVirt instead of
in a Pod. The Sandbox Controller owns the lifecycle
([`SPEC.md` § VMs](../sandbox_controller/SPEC.md)); the guest is
[`runner/vm.nix`](../runner/vm.nix) and its shared configuration schema is [`runner/configuration.py`](../runner/configuration.py);
the template is [`runner_vm.py`](../../cluster/cdk8s/agentplane/runner_vm.py) and the egress relay is
[`launcher_relay.py`](../../cluster/cdk8s/agentplane/launcher_relay.py). The traffic diagram is in
[`agentplane/README.md`](../README.md). Control is the runner's outbound stream
([runner control](runner_control.md)); the guest accepts no connections.

This document holds the constraints that bite an editor of those files, and the alternatives that
lost.

## Credentials stay outside the guest

- KubeVirt derives a launcher's ServiceAccount from a VMI `serviceAccount` volume and automounts its
  token. That volume presents the credential to the guest, so a VM never has one.
  The launcher policy sets the account and disables automount instead.
- The projected tokens are mounted into the relay container and nowhere else in the launcher Pod:
  not `compute`, not the disk helpers. The guest sees only the public config disk, the trust disk and
  placeholder bearers; real upstream credentials stay at the egress gateway.
- A launcher is admitted only when KubeVirt's controller creates it for a live VMI of a live
  VirtualMachine controlled by one of our Sandboxes, and the same Sandbox controls the ServiceAccount.
  Each link is read live and UID-checked: labels and annotations never select an identity.
  Owner references are caller-supplied, so the chain authenticates the launcher, not who may write
  VirtualMachines, VMIs and Pods in the namespace; direct writes there stay restricted by RBAC.
- Relay tokens are short-lived projected tokens that the relay reopens per request, so rotation needs
  no relay restart. Pod deletion is not instantaneous revocation.
- The config and trust disks are ConfigMap disks, snapshots taken at boot. A CA or launch-config
  change reaches a guest only on its next start.

## Guest to relay

- KubeVirt's masquerade interface NATs the guest through the launcher Pod. The guest's `127.0.0.1` is
  not the Pod's, so the relay listens on `0.0.0.0` and the guest dials the masquerade gateway
  (`10.0.2.1`, KubeVirt's default). The runner network policy keeps every other peer off those ports.
- The interface must list at least one port. With none, masquerade forwards every port to the
  guest, including kubelet's probe of the relay's readiness port.
- A guest can open TCP to the egress gateway itself; an unauthenticated request there is refused.
  Proxy environment variables in the guest are convenience, not the boundary.
- The Go runner dials its control and ingress routes directly (they are the relay's own listeners) and
  everything else through the proxy; harness children proxy everything except loopback.
- With Kyverno unavailable, only VM launches in the namespace fail: the policy has its own webhook,
  narrowed by `matchConditions` to launcher creates and to updates of Pods carrying the token volume.

## Guest and storage

- [Sandbox storage layouts](sandbox_storage.md) separates PVC ownership, retained contents and
  compatible Pod/VM attachments. A disk-image PVC is not an ordinary Pod filesystem mount, and
  changing a template never converts an existing volume.
- The state PVC reaches the guest over virtiofs at `/state`, laid out as in a runner Pod. KubeVirt's
  `virtiofsd` is uid/gid 107 with no capabilities, so only guest root and a user with uid and gid 107
  can write there; the runner runs as root and harnesses as 107. Upstream has no rootful alternative.
- The config, launch and trust ConfigMap disks are read-only ISO 9660 images, mounted at
  `/run/agentplane/<disk>`, and `systemd-tmpfiles` links the files the runner reads into
  `/run/agentplane`. A file keeps its ConfigMap's mode, which must stay world-readable: harnesses
  run as uid 107 and read the CA bundle and kubeconfig.
- The runner and the `agentplane` command are not in the guest image. They arrive on the runner disk,
  a read-only erofs on a second `containerDisk` (`//agentplane/runner:disk_image`, [`runner/BUILD.bazel`](../runner/BUILD.bazel)),
  mounted at `/run/agentplane/runner`, and the harness `PATH` lists its `bin` after the system
  profile. The guest system profile also installs the same `preCommitPackages` as the
  `runner-ducktape` Pod image, so the repository hooks and build clients are available on either
  runner type. A runner or CLI release republishes only that small disk. The harnesses and tools are
  still in the guest image.
- The deployed `runner-vm` template declares a Sandbox-owned `BlockFilesystem` workspace with ext4
  directly on a block PVC. `RawDiskImage` remains supported for custom VM workspace claims backed
  by a filesystem PVC. The workspace claim survives stop/start and replacement of the VM object.
  The VM's separate state PVC remains a `DirectoryTree` on a `Filesystem` claim, attached through
  virtiofs; staging puts both VM claims on `piraeus-ssd-2-autodiskful`. The Pod runner keeps its
  Sandbox-owned `DirectoryTree` workspace and existing storage class. Fresh VM claims use the new
  layout; changing the template does not convert retained image-file workspaces. Custom VM
  templates may also declare generic DataVolume disks; those
  remain VM-owned and are not managed workspace declarations. The guest mounts its workspace disk
  with `autoFormat`: `systemd-makefs` creates the ext4 only when `blkid` finds no filesystem or
  swap signature, so any other filesystem fails to mount instead of being rewritten. A disk with
  data but no signature, or with only a partition table, is formatted too; the layouts never produce
  one, and a zero-scan helper that refused it was dropped for that.
- Stopping and starting a VM creates a new VMI on the same VM and disks. The VM supports neither live
  migration nor RAM suspension.
- Commands reach a VM Sandbox only through the control stream. There is no guest exec path; `virt-launcher`
  is not the guest. The operator console is output-only: the integration app attaches the VMI's serial
  console with its own `virtualmachineinstances/console` permission and relays it to a logged-in operator.
  KubeVirt serves one attached client per VMI, so a viewer holds out anyone else, and nothing is replayed.
  An interactive console or a diagnostics path would need its own authorization.
- The guest root is small and replaceable; journals live on the state PVC and agent work on the
  workspace disk. The guest keeps a serial console (`ttyS0`) because a failed boot unit is otherwise
  invisible.

## Fresh VM trial readiness

Basic live acceptance passed on 2026-10-25 PDT against the deployed staging `runner-vm` template:
both fresh 10 GiB claims bound on `piraeus-ssd-2-autodiskful`, with `Block` workspace and
`Filesystem` state modes; the launcher became Ready; and the connected runner reported successful
bootstrap for that incarnation. The bootstrap checked the workspace's ext4 filesystem and wrote,
synced and read back markers in both state and workspace. LINSTOR nodes were online, with
151–361 GiB free on eligible SSD pools and thin-pool metadata below 3% used.

Raw-block lifecycle acceptance on RBE additionally covered suspend/resume, VM replacement,
retained identities and teardown. Evidence: [raw-block lifecycle](https://app.buildbuddy.io/invocation/382077da-f6fa-42ab-b03f-40f8e2491084)
and [two-harness suspend/resume](https://app.buildbuddy.io/invocation/a5c97980-39f1-4dc9-ae7d-7e49271d65f1).

The operator authorized the next fresh VM trial with the complex live drills omitted: node loss,
quorum/fencing, expansion, cross-node movement and recovery remain unqualified in the broader
[Piraeus production plan](../../cluster/docs/plans/piraeus_production.md). This storage smoke check
does not establish deployed cgroup OOM behavior or storage-service recovery guarantees.

## Guest resource controls

The Sandbox is the security isolation boundary, not an individual Session or native harness. A VM
places the Runner, its Sessions, native harnesses and tool children in one guest trust domain. The
same boundary applies to Pod-backed Sandboxes: co-resident Sessions share the Sandbox filesystem,
credentials and ServiceAccount authority ([contract](thread_layering.md#sandbox-isolation-boundary)).
Use separate Sandboxes when workloads need different trust or credential boundaries.

Within a VM, the Runner runs as guest root and native harnesses run as uid/gid 107. Virtiofs presents
`/state` files as 107:107, including the per-Session Runner and Claude state under
`/state/sessions/<session_id>` and the shared Codex home under `/state/codex`. The guest does not create per-Session mount or PID namespaces or hide
same-Sandbox processes from one another. The Runner's state-owner lock and systemd startup fencing barrier
protect storage ownership across Runner restart and ensure harness descendants are reaped; they do not
make Runner internals or sibling Session stores confidential from code in the same Sandbox.

Systemd service cgroups provide resource containment and teardown within one Sandbox principal
([supervision](../runner/docs/supervision.md)).
In isolated Codex mode, the exec-server and its descendants have a sibling budget separate from the
app-server. Claude, local Codex mode, and app-server-owned helpers share their native harness budget.

**Observed failure:** on 2026-10-19, Nix work in staging `public-coder-1` exhausted the 1536 MiB
guest and the guest kernel killed its Codex harness. The guest console recorded
`Out of memory: Killed process 880 (codex)` at `07:30:08.730 UTC`; that PID matches the
[`b46e0a6c-20d3-425a-83cd-f1d70568d2eb` Session](https://agentplane-staging.allegedly.works/#/threads/b46e0a6c-20d3-425a-83cd-f1d70568d2eb),
whose journal recorded exit `137`, `stopped_by_runner=false`, then `PROCESS_LOST`. Three Nix
processes killed in the same episode held about 981 MiB combined anonymous RSS. The deployed image
`35879dd` allowed 3 GiB per harness and 4 GiB aggregate agent memory, both above guest RAM, so those
limits could not preserve guest headroom. This demonstrates that agent work can accidentally kill
its own harness even inside the VM. Budgets that fit guest RAM address guest exhaustion; keeping a
harness alive when its tools exceed their budget also requires a separate tool memory/kill boundary.
The subsequent conversation-recovery failure is addressed separately in
[PR #11091](https://github.com/agentydragon/ducktape/pull/11091).

- The VM template and guest runner share the typed `GuestResources` policy. The template
  takes its KubeVirt memory request and vCPU count from that object; an `ExecStartPre` reads the same
  seed and applies the shared `agentplane.slice` caps after the ConfigMap disk is mounted and before the Runner
  starts. The staging template specifies:

  | Scope               | Memory                                                                                  | CPU                              | PID limit | Block I/O limit                                          |
  | ------------------- | --------------------------------------------------------------------------------------- | -------------------------------- | --------- | -------------------------------------------------------- |
  | VM guest            | 8192 MiB (8,589,934,592 bytes), with a 1024 MiB (1,073,741,824 byte) OS reserve         | 1 vCPU                           | —         | —                                                        |
  | Shared parent slice | `MemoryHigh` 6144 MiB (6,442,450,944 bytes), `MemoryMax` 7168 MiB (7,516,192,768 bytes) | 100,000 µs per 100,000 µs period | 2048      | —                                                        |
  | Harness aggregate   | `memory.max` 2048 MiB (2,147,483,648 bytes)                                             | 100,000 µs per 100,000 µs period | 768       | read 50 MiB/s and 1000 IOPS; write 20 MiB/s and 500 IOPS |
  | Native process leaf | `memory.max` 1536 MiB (1,610,612,736 bytes)                                             | 100,000 µs per 100,000 µs period | 512       | read 50 MiB/s and 1000 IOPS; write 20 MiB/s and 500 IOPS |

  The sibling executor aggregate and its leaf each cap memory at 3072 MiB (3,221,225,472 bytes),
  CPU at one vCPU and PIDs at 512, with the same I/O caps. Its optional seed budget selects isolated
  tools; see [Codex executor isolation](../runner/docs/codex_executor.md) for lifecycle and scope.

  Memory swap remains disabled. Aggregate and native-process cgroups use `memory.oom.group=0`,
  so a cgroup OOM selects individual process victims. A tool can be killed while its harness or
  exec-server survives; the kernel can also select the native leader. Leader exit still triggers
  systemd cleanup of all descendants. Runner records `memory.events` and `memory.peak` as JSON
  before releasing the service. This keeps each harness's
  kill boundary below the Runner.

  The CI-active k3s VM scenario `test_codex_tool_oom_preserves_runner_and_app_server` runs a real
  Codex shell command past the executor limit and asserts the kernel OOM counter increments once.
  Codex receives exit 137, the native command item is `failed` with that exit code, and the turn
  completes normally. The same Runner boot and app-server process/PID survive; a follow-up turn
  executes successfully in the same executor leaf. The allocator raises its own `oom_score_adj`
  to make selection of the tool deterministic. This covers the production Runner and pinned Codex
  inside the test guest, independently of further guest slimming. It does not exercise the
  production NixOS systemd caps, executor death/restart, or a breached outer service limit;
  [fixture details and artifacts](../system_tests/k3s/README.md#vm-sandboxes) describe the scope.

- [Tool memory isolation](harness_tool_memory_isolation.md) records optional research on putting
  tighter resource limits around a tool child. That would be a separate resource policy, not a change
  to the Sandbox security boundary.
- The Go harness supervisor retains the state-owner descriptor, forwards stop signals and reaps
  descendants, so a Runner restart cannot open the same state while old native work continues
  ([runner README](../runner/README.md#sqlite-storage)).
- Agent processes get no unrestricted guest root and no cgroup administration, so they cannot alter
  guest services or the configured resource budgets. This does not protect Runner-owned state from
  other code in the same Sandbox; the journal is not tamper-proof against agent code.
- The launcher's memory limit covers guest RAM plus QEMU overhead, disk helpers and the relay;
  a limit equal to guest RAM is not isolation. Guests need KVM-capable nodes (`kubevirt.io/schedulable`); do not fall back to
  software emulation.
- A cold start on an early guest took two minutes to pull the image, four to scan and format
  its blank disks and three to build a Java trust store one certificate at a time (the trust disk now
  supplies the PKCS#12 store). Per-boot work proportional to disk size or certificate count shows up
  directly as startup latency. So does the guest closure: a node pulls the whole qcow2 before the
  guest's first boot, which is why `vm.nix` ships no documentation or channel copy and trims codex's
  voice host, and why a new guest package is paid for on every cold node.

## Alternatives that lost

Each was costed against KubeVirt v1.8.2 for adding the relay and its token mounts to the launcher.

- **KubeVirt hook sidecar.** The launcher waits for each declared hook socket and calls gRPC `Info`,
  so a plain HTTP relay would stall startup until timeout. The `HookSidecar` schema offers ConfigMap
  and PVC mounts only, not a projected ServiceAccount-token volume, so a wrapper still needs
  admission for the mounts. The legacy hook API is deprecated in v1.9; its Plugins API still injects
  through an admission policy or webhook.
- **`MutatingAdmissionPolicy`.** CEL cannot read the VMI and VM live as a webhook can, so the owner
  chain would need a separately enforced trusted mapping.
- **Native KubeVirt support.** v1.8.2 has no launcher ServiceAccount selection and no proxy-only
  projected volumes; it needs an upstream change or a maintained patch.
- **A companion proxy Pod per environment.** The authenticated Pod becomes the proxy Pod, which needs
  a private guest-to-proxy route that enforces environment identity and revocation coupled to the VM.
- **A `serviceAccount` volume for the launcher's identity.** It hands the credential to the guest
  (above).
- **An implicitly portable guest disk.** A disk image on a filesystem PVC and a raw block filesystem
  need different Pod attachments from an ordinary directory tree. Block storage remains an option,
  including an explicit stopped-Sandbox conversion; it does not make kind switches compatible by
  itself. The [storage layout contract](sandbox_storage.md) defines that boundary.
