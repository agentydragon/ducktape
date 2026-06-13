# NixOS + RBE Bazel: Required Configuration

Running Bazel with BuildBuddy RBE on a NixOS host needs three flags that a
stock FHS Linux host does not. They reconcile a NixOS client (nix-store paths,
patched bazel, no `/bin/bash`) with Ubuntu RBE workers (FHS paths, native
`/bin/bash`). Managed by `nix/nixos/modules/bazel-nixos/` (written to
`/etc/bazel.bazelrc`); the workspace `.bazelrc` carries the strict-action-env
flag for all users.

| Flag                                                             | Why                                                                                                                                                                                                       |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--shell_executable=/bin/bash`                                   | nixpkgs patches `bazel_8`'s shell to a nix-store path that doesn't exist on RBE workers. `/bin/bash` exists on NixOS (system profile symlink) and natively on workers.                                    |
| `--incompatible_strict_action_env`                               | Default in Bazel 9, **not** Bazel 8. Without it the full NixOS client PATH (dozens of `/nix/store/...` entries) leaks into action envs sent to RBE, where those paths absent.                             |
| `--host_action_env=PATH=...` (+ `NIX_LD`, `NIX_LD_LIBRARY_PATH`) | Scopes NixOS paths to **host/exec-config tools that always run locally** — never `--action_env`, which would leak them to RBE. nix-ld vars let Bazel-downloaded dynamically-linked binaries run on NixOS. |

**Deviation from stock Bazel:** an FHS host needs none of these — `/bin/bash`
exists, strict action env is the Bazel-9 default, and there are no nix-store
paths to keep off the workers. On NixOS each is load-bearing.

For per-flag env-var scoping rationale (`--test_env`, `--repo_env`, why not
`--action_env`), the genrule PATH gap, and toolchain FHS symlink issues, see
the original investigation in `debug/nixos_bazel_bash/README.md`.
