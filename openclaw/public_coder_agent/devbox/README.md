# Public-coder devbox

This directory owns the Nix configuration for the public-coder agent's
build/test VM:

- `nixos.nix` configures its NixOS host.
- `home.nix` configures the `coder` user's Home Manager profile.
- `container-disk.nix` packages the generated qcow2 as a KubeVirt containerDisk.

The `public-coder-devbox` NixOS configuration is registered in `flake.nix`, and
its `.#public-coder-devbox-container-disk` output is indexed in
`nix/flake/packages.nix`. The deployment resources stay with their owner under
`cluster/parked/public-coder-agent/devbox/`.

No workflow builds this output while Public Coder is parked. Resuming moves
`public-coder-devbox-image.yml` back from `cluster/parked/public-coder-agent/ci/`; it published
the containerDisk to the private Forgejo registry on `devel`, where Flux's
ImagePolicy consumes the tags.
