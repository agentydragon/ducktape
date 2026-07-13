# Bazel Configuration and BuildBuddy RBE

This document is the source of truth for Bazel rc ownership and execution
policy in Ducktape. Update it with any change to `.bazelrc`, the NixOS Bazel
module, BuildBuddy credential generation, or CI/session Bazel setup.

## Policy

All Ducktape build-like commands (`build`, `test`, and `run`) enable the
workspace's `rbe` config. Eligible actions execute only on BuildBuddy: Bazel
must fail rather than fall back to local execution. `bazel run` still launches
the completed binary locally, and Bazel-internal repository/symlink work remains
local.

The configuration layers have separate owners:

| Layer      | Owner                                                 | Contents                                                                                                 |
| ---------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| System     | `nix/nixos/modules/bazel/system.bazelrc`              | NixOS local shell, host/test PATH, and nix-ld environment                                                |
| User       | Home Manager's `~/.bazelrc`                           | UI preferences, local disk/repository caches, and the BuildBuddy credential import                       |
| Credential | `nix/home/modules/buildbuddy.nix` or session/CI setup | Only `common:rbe --remote_header=...`                                                                    |
| Workspace  | `devinfra/bazel/rbe.bazelrc`, imported by `.bazelrc`  | Ducktape's default `--config=rbe`, endpoints, execution platform, remote-only strategy, and worker shell |

Credentials do not select RBE and are not sent unless a repository enables the
`rbe` config. Conversely, Ducktape selects RBE even when no credential is
available; the build then fails authentication instead of silently executing
locally.

## Shell paths

Shell actions use paths from their execution environment:

- Local NixOS configuration uses
  `/run/current-system/sw/bin/bash`, which remains stable across system
  generations.
- Ducktape's RBE configuration overrides it with `/bin/bash`, which exists on
  the Ubuntu BuildBuddy workers.

`--shell_executable` is configuration-wide; it cannot choose a different path
after Bazel decides where an action runs. This is why Ducktape forbids local
fallback from its RBE configuration.

`--incompatible_strict_action_env` prevents Nix store paths from leaking into
remote actions. NixOS-only `PATH`, `NIX_LD`, and `NIX_LD_LIBRARY_PATH` values
remain scoped to local host/test/repository work in the system rc.

## Entry points

Within Ducktape, direct `bazelisk`, `bb run`, CI, Codex, and Claude sessions all
inherit the same workspace RBE policy. Entry points may add metadata, quiet
output, test filters, or credentials; they must not duplicate RBE selection,
endpoints, strategy, or shell configuration.

`bbr` remains the preferred agent command for build/test/query because it runs
the outer Bazel invocation on a BuildBuddy runner. `bazelisk run` and `bb run`
build through RBE but execute the requested binary on the caller.

## Verification

`devinfra/nixos_bazel_test/run.sh test` is the end-to-end contract. It builds and
boots the real `bazel-test` NixOS image, seeds a miniature workspace with the
production `rbe.bazelrc`, and supplies a scoped BuildBuddy credential. Its Python
target uses the real `aspect_rules_lint` Ruff aspect that originally exposed the
NixOS `/bin/bash` failure. The test verifies from Bazel's execution log that the
Ruff action ran remotely before the executable ran locally.

`.github/workflows/nixos-bazel-rbe.yml` runs this integration test for relevant
internal pull requests, pushes to `devel`, and manual dispatches. GitHub cannot
provide the SOPS decryption secret to fork pull requests, so those runs are
skipped.

For cache layout and worktree mechanics, see
<bazel_worktree_cache_sharing.md>. For `bb remote`'s outer-runner behavior, see
<bb_remote_internals.md>. Historical NixOS compatibility investigation is in
<../../debug/nixos_bazel_bash/README.md>.
