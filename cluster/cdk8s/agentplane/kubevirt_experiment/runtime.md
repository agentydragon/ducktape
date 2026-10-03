# Runner guest prototype

This is the guest-runtime part of the [VM experiment](README.md). The platform
experiment used Fedora; its successful admission and network probes do not prove
that this NixOS runner image boots or recovers sessions. Keep live results in
[`agentplane/debug/kubevirt/`](../../../../agentplane/debug/kubevirt/).

## Image and boot contract

Build `.#agentplane-runner-vm-container-disk` from this checkout's
`//agentplane/runner:runner_wheel`. The **Agentplane Runner VM Image** workflow builds
that wheel, supplies it through `DUCKTAPE_ARTIFACT_OVERRIDES`, and builds the qcow2
containerDisk. Its normal registry publication is restricted to `devel`. A local
build without the override uses the repository's pinned wheel, which may predate
the guest entry points during development.

The guest uses EFI with Secure Boot disabled, an ephemeral root containerDisk,
and the following virtio disks:

| Disk serial         | Contents                                                                   | Guest use                                                                              |
| ------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `agentplane-config` | ConfigMap files `config.json`, `kubeconfig`                                | Copied to `/run/agentplane`; contains public configuration and credential placeholders |
| `agentplane-trust`  | trust-manager ConfigMap files `ca-certificates.crt`, `ca-certificates.p12` | Copied to `/run/agentplane`; shared PEM and passwordless PKCS#12 trust stores          |
| `state`             | Retained XFS filesystem, label `APSTATE`                                   | `/state`; runner journals and quota-limited native histories                           |
| `workspace`         | Retained ext4 filesystem, label `APWORKSPACE`                              | `/workspace`; checkouts, home, and tool caches                                         |

Attach the existing trust-manager ConfigMap in the VM namespace as a read-only
configuration disk with serial `agentplane-trust`. The guest uses its PEM bundle
unchanged and gives Java
its passwordless PKCS#12 store, without rebuilding either at boot. Both files must
be nonempty for startup to proceed. ConfigMap disks are boot snapshots: stop/start
the VM after a bundle update, including CA rotation.

Add the disk and volume to the VMI template alongside the public config disk:

```yaml
spec:
  template:
    spec:
      domain:
        devices:
          disks:
            - name: trust
              disk:
                bus: virtio
              serial: agentplane-trust
      volumes:
        - name: trust
          configMap:
            name: agentplane-testing-egress-ca
```

Use the ConfigMap for the VM's egress gateway, containing both
`data.ca-certificates.crt` and `binaryData.ca-certificates.p12` from trust-manager.
The example name is for the testing namespace.

The guest validates schema version 1 before preparing disks. Formatting requires
explicit `format_blank_disks` authorization, no filesystem signatures, and a
complete zero scan. Recognized retained filesystems must have the expected type
and label. The provider removes formatting authorization after a successful runner
RPC. A missing, nonzero, or unexpected disk fails startup; it is not repaired by
formatting it.

The runner listens on port 7000. Only that port is forwarded by the production VM
template. The guest has no SSH server. The relay remains outside the guest, reached
through `http://10.0.2.1:3128`; projected Kubernetes tokens stay in the relay.

## Build evidence

On 2026-10-03, a local Nix build completed package import checks and produced the
containerDisk archive at `/nix/store/xrglvry0mw2y2pm5yxgf0mkvvx14infq-agentplane-runner-vm.tar.gz`
(1.6 GiB). It used the runner wheel downloaded from
[BuildBuddy invocation `4dc32950-b4e6-5e94-a71b-dfe6a33e648d`](https://app.buildbuddy.io/invocation/4dc32950-b4e6-5e94-a71b-dfe6a33e648d).
This verifies package import and image assembly, not guest boot or runtime behavior.
Archive inspection found one layer containing only `./disk/disk.qcow2` (owner
UID/GID 107, size 4,725,407,744 bytes) and no dangling Nix-store symlink.

The subsequent [live runner experiment](../../../../agentplane/debug/kubevirt/runtime-20261003.md)
records the corrected OCI image, boot failures and fixes, actual native CLI turns,
effective resource limits, pressure probes, and retained-state recovery evidence.
Keep those results separate from the broader acceptance checklist below.

## Boundaries to exercise

The service runs as UID 1001. Native harnesses, initialization, and setup scripts
run as UID 1000 in delegated cgroups. The supervisor retains the state ownership
descriptor while terminating native work. The runner journal tree is inaccessible
to UID 1000; native histories live under `/state/native` with shared group ACLs and
an XFS project quota. Workspace writes consume the separate workspace disk.

The current guest has a 7 GiB service memory ceiling, a 4 GiB aggregate workload
ceiling, and a 3 GiB per-process-group ceiling. CPU, process-count, and disk I/O
limits also apply. The default provider template requests 10 GiB guest memory.
These are configured limits; confirm their effective values inside a booted guest.

## Live acceptance

Use a disposable namespace, fresh disks, and a digest-pinned image built from the
PR revision. Increase the small Fedora experiment's resource quota for this guest.
Do not attach an existing environment's state. Use the provider's create path so
the test covers the actual config disk and owner references.

1. **Boot:** require both VMI readiness and a real `ListSessions` RPC. Inspect unit
   failures, filesystem labels, mount options, and effective cgroup limits. Record
   the image digest and VM/VMI/launcher UIDs. Do not collect token contents.
2. **Permissions and egress:** use the runner's `Initialize` RPC for a deterministic
   script that reports UID/GID and cgroup membership, writes `/workspace`, and proves
   `/state/sessions` and cgroup controls are inaccessible. Confirm the existing
   proxy and CA configuration with production gateway credential substitution and
   CONNECT, including unauthorized requests and peer-VM denial.
3. **Native sessions:** start Claude and Codex sessions in child directories of
   `/workspace`. Confirm their native histories are writable and the runner's
   journals remain protected. Cancel a long-running tool; verify its descendants
   leave the workload cgroup and the runner still serves RPCs.
4. **Resource pressure:** in fresh disposable sessions, trigger workload memory,
   process, native-history quota, and workspace capacity limits separately. Record
   the effective limit and the observed failure. After each failure, require a
   responsive runner and readable journal. A launcher-only readiness check is
   insufficient.
5. **Recovery:** record a workspace file checksum, session source/cursor, and native
   continuation identity. Stop fully, wait for the old VMI to disappear, start, and
   reattach. Repeat after killing the runner and after an explicit approved image
   replacement while halted. Require retained data and coherent event replay; do
   not infer recovery from the PVC merely remaining present.
6. **Fencing:** attempt a second runner against the same state while the original
   runner or its supervisor still holds ownership. Verify rejection; then verify
   startup succeeds after native writers are gone. RWO attachment alone does not
   establish this property.

Initialization selects one durable script per environment. Use a fresh environment
for a different initialization script; an exact retry replays its recorded result.
For boot failures before RPC availability, inspect serial boot output and launcher
events. Any diagnostic guest variant should be recorded separately from acceptance
of the final image.

Delete only the disposable experiment's VM, retained DataVolumes/PVCs, namespace,
and separately created cluster-scoped admission/secret-store resources. Normal VM
deletion deliberately retains state disks; experiment cleanup must account for
them explicitly.
