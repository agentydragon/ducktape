# Environment images

## VM guest mount contracts

[`vm_guest_contract.json`](vm_guest_contract.json) records the KubeVirt disk serials and guest
mount paths shared by the NixOS guest and the rendered VM template. The Nix module uses it to build
the evaluated `fileSystems` table, and a NixOS assertion checks those mounts against the contract.
[`firecracker_guest_contract.json`](firecracker_guest_contract.json) records Firecracker's fixed
virtio-blk device order and the same runner mount paths. The Nix guest module and rendered
Firecracker template consume this contract; the NixOS assertion checks every mount against it.

The executable erofs disk keeps the KubeVirt serial `agentplane-runner` and mounts at
`/run/agentplane/environment`. The config disk contains `environment-runtime.json` and
`environment-runtime.env`; the launch disk mounts at `/run/agentplane/sandbox`. The Runtime TLS
Secret stays behind a root-only private virtiofs directory, with the public Runtime path exposed as
a symlink. The trust-manager bundle mounts separately as public trust data.

The Firecracker contract assigns `vda` to the raw ext4 NixOS root and `vdb` through `vdg` to the
runner erofs, persistent state, workspace, config, launch and egress trust disks.
The launcher creates the state and workspace ext4 filesystems; the guest does not auto-format them.
The root guest agent repairs time through guest-management gRPC on privileged vsock port 52 and
subscribes to the host's SPIFFE Workload API over a separate connection on privileged port 53.
It serves credentials on a root-only local Unix socket, which Runtime consumes with go-spiffe's
in-memory `X509Source`. Cold boot and thaw require a fresh subscription before network admission;
normal rotations flow over the stream without restarting Runtime. `systemd-networkd` waits for
the first synchronization acknowledgement before configuring the fixed guest addresses.

## Release and image inputs

Bazel builds the runner disk image (`//agentplane/env/images:disk_image`) from source at the same commit
as the runner. The VM guest does not embed the runner: the image packs it, the executor companion
and the `agentplane` command into a read-only erofs (`disk/runner.img`) that the `environment-vm` template
attaches beside the guest; the same image also carries them as plain files under `bin/`, which the
runner Pod templates mount at `/run/agentplane/environment` and start the runner and companion from, so
the Pod's tools image (`image.nix`) holds neither. The k3s fixture mounts the same image. A runner,
companion or CLI change therefore republishes the runner disk image but not the `runner` tools
image. The `agentplane-cli` artifact pin still feeds the Action sandbox images, which rebuild on
any pin bump.

`nix build .#agentplane-environment-firecracker-image` builds the native NixOS Firecracker guest as
an OCI archive whose root contains `vmlinux`, a partitionless raw ext4 `rootfs.ext4`, and a text
`init` file holding the NixOS init store path. The launcher mounts the image at `/images/boot`; the
full NixOS closure is copied into the guest root, while the replaceable runner remains on its own
read-only erofs disk. Firecracker resume/snapshot lifecycle belongs to the host launcher; the guest
thaw service updates time and TLS material without restarting Runtime or its harnesses.

## KubeVirt VM workspace disk

The guest declares `/state/workspaces` in NixOS `fileSystems` as ext4 with `autoFormat`:
`systemd-makefs` creates the filesystem when `blkid` finds no signature and skips any disk that
has one, so a disk holding data but no recognized signature is formatted too
([why that is accepted](../../docs/vm_sandboxes.md#guest-and-storage)).

`systemd.tmpfiles` owns the mounted workspace directory's `agent:agent` ownership, setgid mode and
default ACL. Before Environment Runtime starts, a separate service runs as `agent` and moves an
existing `/state/workspaces/.home` to `/state/home`, or creates the canonical home with mode `0700`.
The state PVC is mounted before this move, and a collision fails closed without merging either tree.

## Ducktape development image

`nix build .#environment-tools-image` builds the `environment-tools` container every staging Pod Sandbox
runs. It reuses this runner, both harnesses, and the sandbox substrate, adding `preCommitPackages`
from the shared flake tool definitions and the repo-configured Gazelle. This includes `bb`, `bbr`,
Bazelisk, pre-commit and the pinned formatters without duplicating their package/version list.
Tools arrive without changing the runner executable or the internal service-account and
network-policy identities, so it stays the environment's one Pod execution environment.

`nix build .#checks.x86_64-linux.environment-tools` exercises representative repository
hooks in a sandboxed Nix derivation, using the exact tool closure included in the image. The
image workflow runs that check and a thin non-root container smoke test for the assembled
filesystem, entrypoint and tool availability. These prove local tooling, not authenticated BuildBuddy execution;
a deployed sandbox must separately prove the normal proxy/CA/credential route for `bbr`.
No credentials are embedded in the image. The staging container workflow is BuildBuddy-hosted
builds, not a local Bazel client with remote actions. Local Bazel execution is deferred until
VM-backed environments; package availability does not imply permission to run it. Adding tools
does not itself establish a tool memory boundary. Isolated Codex execution is configured separately
([executor lifecycle and scope](../runtime/execution/docs/codex_executor.md)). See the
[BuildBuddy execution policy and validation](../../docs/buildbuddy_remote_auth.md#validation-and-execution-policy).
