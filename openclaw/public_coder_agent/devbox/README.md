# Public-coder devbox

This directory owns the Nix configuration for the public-coder agent's
build/test VM:

- `nixos.nix` configures its NixOS host.
- `home.nix` configures the `coder` user's Home Manager profile.
- `container-disk.nix` packages the generated qcow2 as a KubeVirt containerDisk.

The `public-coder-devbox` NixOS configuration is registered in `flake.nix`, and
its `.#public-coder-devbox-container-disk` output is indexed in
`nix/flake/packages.nix`. The deployment resources stay with their owner under
`cluster/k8s/agents/public-coder-agent/devbox/`.

`.github/workflows/public-coder-devbox-image.yml` builds this output on pushes
to `devel` when the devbox recipe, its shared NixOS modules, or the flake
registration changes. It publishes the containerDisk to the private Forgejo
registry. It also accepts manual dispatch from any ref, but publishes only on
`devel`, where Flux's ImagePolicy consumes the tags.
