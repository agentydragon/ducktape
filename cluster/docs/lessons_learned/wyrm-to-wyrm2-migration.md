# wyrm → wyrm2 Migration (Completed 2026-03)

Pop!\_OS VM (wyrm) replaced by NixOS VM (wyrm2) with 2x RTX 5090 GPUs. wyrm2 is a K8s worker joined via `k8s-worker.nix` + Nebula mesh.

Migration covered: SSH keys, kubeconfig, secrets, app data, code repos, Syncthing, browser profiles. All data moved and verified. wyrm decommissioned.

See `nix/nixos/modules/k8s-worker.nix` for wyrm2's configuration.
