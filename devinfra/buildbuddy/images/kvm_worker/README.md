# BuildBuddy KVM worker image

This image is a small EFI NixOS containerDisk for one static KubeVirt VM. The VM mounts its
80 GiB `work` disk at `/buildbuddy`, where Docker stores layers and the BuildBuddy executor
keeps its cache and workspaces. A read-only `bbkey` ISO supplies `api-key` at boot; the guest
passes it to the executor through a mode-0600 file under `/run`, and the Nix store contains only
the environment-variable placeholder.

The executor is the deployed Helm version `enterprise-v2.321.0`, pinned to its verified amd64
manifest digest in `nixos.nix`. Docker is the sole action isolation mode. Actions receive
guest-root UID/GID plus `SYS_ADMIN`, `NET_ADMIN`, `MKNOD`, and guest `/dev` devices for nested
KVM; the guest loads the AMD nested-KVM, loop, TUN, overlay, bridge, and netfilter modules.
GHCR action image names are rewritten through the in-cluster Zot cache.

Root test code can compromise the guest and its BuildBuddy key. This worker is for trusted code
only; it does not isolate mutually untrusted jobs from one another. If guest trust is lost,
recreate the VM and rotate the key.

The KubeVirt network fence must allow DNS, `remote.buildbuddy.io:443`, `buildbuddy.bbcr.io:443`,
and Zot's backend TCP port `5000`. GHCR pulls use Zot's internal HTTP endpoint, so actions do not
need direct GHCR egress.
