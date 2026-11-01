# Environment images

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

## VM workspace disk

The guest declares `/state/workspaces` in NixOS `fileSystems` as ext4 with `autoFormat`:
`systemd-makefs` creates the filesystem when `blkid` finds no signature and skips any disk that
has one, so a disk holding data but no recognized signature is formatted too
([why that is accepted](../../docs/vm_sandboxes.md#guest-and-storage)).

`systemd.tmpfiles` owns the mounted workspace directory's `agent:agent` ownership, setgid mode,
default ACL and private `.home` directory.

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
