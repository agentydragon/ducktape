# NixOS RBE Worker Experiment

Experimental NixOS-based BuildBuddy worker with systemd, envfs, and nix-ld.
Its NixOS module, shared worker package list, and flake output definitions live
in this directory. The root flake imports those outputs so the worker remains
available as `nixosConfigurations.nix-rbe-worker` and `.#nix-rbe-nixos`.

Build and import the tarball:

```bash
nix build .#nix-rbe-nixos
docker import result/tarball/*.tar.xz nix-rbe-nixos
```

The experiment is not ready for BuildBuddy Firecracker workers: goinit pivots
into the root filesystem without running `/init`, so systemd does not activate
envfs or nix-ld. See [the Firecracker execution model](../../devinfra/docs/bb_remote_internals.md).
The separate [dockerTools variant](../nix_rbe_image/README.md) is documented
beside its implementation.
