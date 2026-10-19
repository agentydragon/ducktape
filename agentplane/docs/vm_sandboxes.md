# VM Sandboxes

A `SandboxTemplate` with `vm` runs its Sandbox's runner in a NixOS guest under KubeVirt instead of
in a Pod. The Sandbox Controller owns the lifecycle
([`SPEC.md` § VMs](../sandbox_controller/SPEC.md)); the guest is
[`runner/vm.nix`](../runner/vm.nix) and its seed is [`runner/guest_config.py`](../runner/guest_config.py);
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
- The runner dials its control and ingress routes directly (they are the relay's own listeners) and
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
- The deployed `runner-vm` template declares a Sandbox-owned `RawDiskImage` workspace with ext4.
  Its claim survives stop/start and replacement of the VM object. The Pod runner uses a
  Sandbox-owned `DirectoryTree` workspace. Custom VM templates may still declare generic
  DataVolume disks; those remain VM-owned and are not managed workspace declarations. The guest
  mounts its workspace disk with `autoFormat`: `systemd-makefs` creates the ext4 only when `blkid`
  finds no filesystem or swap signature, so any other filesystem fails to mount instead of being
  rewritten. A disk with data but no signature, or with only a partition table, is formatted too; the
  layouts never produce one, and a zero-scan helper that refused it was dropped for that.
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

## Guest resource controls

The Sandbox is the security isolation boundary, not an individual Session or native harness. A VM
places the Runner, its Sessions, native harnesses and tool children in one guest trust domain. The
same boundary applies to Pod-backed Sandboxes: co-resident Sessions share the Sandbox filesystem,
credentials and ServiceAccount authority ([contract](thread_layering.md#sandbox-isolation-boundary)).
Use separate Sandboxes when workloads need different trust or credential boundaries.

Within a VM, the Runner runs as guest root and native harnesses run as uid/gid 107. Virtiofs presents
`/state` files as 107:107, including the per-Session Runner and native state under
`/state/sessions/<session_id>`. The guest does not create per-Session mount or PID namespaces or hide
same-Sandbox processes from one another. The Runner's state-owner descriptor and process supervisor
protect storage ownership across Runner restart and ensure harness descendants are reaped; they do not
make Runner internals or sibling Session stores confidential from code in the same Sandbox.

The delegated cgroups below provide resource containment and teardown, not security isolation between
a harness and its tool children. A native harness and the tools it starts share that harness's cgroup
and resource budget. No separate per-tool budget or kill boundary is part of the current VM contract.

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

- The runner service is a systemd unit with `MemoryHigh`/`MemoryMax`, `CPUQuota` and `TasksMax`, and
  delegates a subtree. `agentplane.runner.cgroups` puts agent work under an aggregate group and each
  native harness under its own, with memory, CPU, task and I/O limits and `memory.oom.group`.
- When a harness subtree exceeds its memory budget, `memory.oom.group` kills that harness and its
  descendants; the Runner remains outside that group and can continue answering RPCs. Acceptance of
  the VM cgroup limits is still open.
- [Tool memory isolation](harness_tool_memory_isolation.md) records optional research on putting
  tighter resource limits around a tool child. That would be a separate resource policy, not a change
  to the Sandbox security boundary.
- The Rust harness supervisor retains the state-owner descriptor, forwards stop signals and reaps
  descendants, so a Runner restart cannot open the same state while old native work continues
  ([runner README](../runner/README.md#sqlite-storage)).
- Agent processes get no unrestricted guest root and no cgroup administration, so they cannot alter
  guest services or the configured resource budgets. This does not protect Runner-owned state from
  other code in the same Sandbox; the journal is not tamper-proof against agent code.
- The launcher's memory limit covers guest RAM plus QEMU overhead, disk helpers and the relay;
  a limit equal to guest RAM is not isolation. `agentplane-testing`'s 4Gi container limit caps a guest at
  about 1.7Gi. Guests need KVM-capable nodes (`kubevirt.io/schedulable`); do not fall back to
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
