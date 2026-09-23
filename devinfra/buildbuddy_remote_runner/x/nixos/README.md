# NixOS BuildBuddy Remote Runner Experiment

Experimental BuildBuddy Remote Runner image based on NixOS, systemd, envfs, and
nix-ld. Its NixOS module and flake output definitions live here; the shared
package list is in [shared BuildBuddy Nix image package list](../../../buildbuddy_nix_image_packages.nix).
The root flake registers the outputs lazily as
`nixosConfigurations.buildbuddy-remote-runner` and
`.#buildbuddy-remote-runner-nixos-image`.

Build and import the tarball:

```bash
nix build .#buildbuddy-remote-runner-nixos-image
docker import result/tarball/*.tar.xz buildbuddy-remote-runner-nixos-image
```

The experiment is not ready for BuildBuddy Remote Runners on Firecracker:
goinit pivots into the root filesystem without running `/init`, so systemd does
not activate envfs or nix-ld. See [the Firecracker execution model](../../../../devinfra/docs/bb_remote_internals.md).
The separate [Nix RBE container image variant](../../../rbe_container_image/x/nix/README.md)
is documented beside its implementation.
