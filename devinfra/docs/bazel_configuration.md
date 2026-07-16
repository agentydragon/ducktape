# Bazel Configuration and BuildBuddy RBE

This document is the source of truth for Bazel rc ownership and execution
policy in Ducktape. Update it with any change to `.bazelrc`, the NixOS Bazel
module, BuildBuddy credential generation, or CI/session Bazel setup.

## Policy

All Ducktape build-like commands (`build`, `test`, and `run`) enable the
workspace's `rbe` config. Build, lint, typecheck, and normal test actions execute
only on BuildBuddy: Bazel must fail rather than fall back locally. The sole
spawn exception is `TestRunner` for an explicitly local non-Docker live test.
`bazel run` launches the completed binary on the Bazel client, and
Bazel-internal repository/analysis work stays in that client process.

The configuration layers have separate owners:

| Layer      | Owner                                                 | Contents                                                                        |
| ---------- | ----------------------------------------------------- | ------------------------------------------------------------------------------- |
| System     | `nix/nixos/modules/bazel/system.bazelrc`              | NixOS local shell, host/test PATH, and nix-ld environment                       |
| User       | Home Manager's `~/.bazelrc`                           | UI preferences, local disk/archive caches, and the BuildBuddy credential import |
| Credential | `nix/home/modules/buildbuddy.nix` or session/CI setup | Only `common:rbe --remote_header=...`                                           |
| Workspace  | `devinfra/bazel/rbe.bazelrc`, imported by `.bazelrc`  | RBE selection/endpoints, action placement, and worker shell                     |

Credentials do not select RBE and are not sent unless a repository enables the
`rbe` config. Conversely, Ducktape selects RBE even when no credential is
available; the build then fails authentication instead of silently executing
locally.

## Action placement

“Local” always means local to the Bazel client, which differs by entry point:

| Work class                                    | Direct `bazelisk` / `bb` on NixOS or FHS | `bbr` outer FHS runner    | BuildBuddy execution worker |
| --------------------------------------------- | ---------------------------------------- | ------------------------- | --------------------------- |
| Module extensions and repository rules        | Runs in the client                       | Runs in the outer runner  | Never                       |
| Analysis and Bazel-internal file/symlink work | Runs in the client                       | Runs in the outer runner  | Never                       |
| Build, codegen, Ruff, mypy, and aspect spawns | Submitted by the client                  | Submitted by outer runner | Runs here                   |
| Regular `TestRunner`                          | Submitted by the client                  | Submitted by outer runner | Runs here                   |
| Non-Docker live `TestRunner`                  | Runs in the client                       | Runs in the outer runner  | Never                       |
| Docker live `TestRunner`                      | Submitted by the client                  | Submitted by outer runner | Runs here                   |
| Completed `bazel run` executable              | Runs in the client                       | Runs in the outer runner  | Never                       |

Bazel normally copies a target's `no-remote-exec` tag to every spawn created
for that target, including `PyWriteBuildData`, Ruff, and mypy aspect actions.
The RBE config removes that execution requirement from every mnemonic except
`TestRunner`, then gives `TestRunner` the ordered strategies `remote,local`.
An ordinary test remains remote; an explicitly local test rejects the first
strategy and selects the second. This is eligibility-based selection, not
failure fallback: `--remote_local_fallback=false` still prevents an RBE failure
from retrying locally.

The `live_openai_py_test` macro marks only non-Docker `.live` variants local.
Variants with `requires_docker = True` remain fully remote. CI builds all live
targets so their lint/typecheck actions stay covered, while
`--test_tag_filters=-live_openai_api` prevents credentialed test execution.

## Shell paths

Shell actions use paths from their execution environment:

- Local NixOS configuration uses
  `/run/current-system/sw/bin/bash`, which remains stable across system
  generations.
- Ducktape's RBE configuration overrides it with `/bin/bash`, which exists on
  the Ubuntu BuildBuddy workers.

`--shell_executable` is configuration-wide; it cannot choose a different path
after Bazel decides where a build action runs. This is why Ducktape forbids
local fallback from its RBE configuration. A selected local `TestRunner`
launches its test executable directly; the NixOS test `PATH` ensures
`#!/usr/bin/env bash` launchers find the Nix shell.

`--incompatible_strict_action_env` prevents Nix store paths from leaking into
remote actions. NixOS-only `PATH`, `NIX_LD`, and `NIX_LD_LIBRARY_PATH` values
remain scoped to local host/test/repository work in the system rc.

## Entry points

Within Ducktape, direct `bazelisk`, `bb run`, CI, Codex, and Claude sessions all
inherit the same workspace RBE policy. Entry points may add metadata, quiet
output, test filters, or credentials; they must not duplicate RBE selection,
endpoints, strategy, or shell configuration.

`bbr` remains the preferred agent command for build/test/query because it runs
the outer Bazel invocation on a BuildBuddy runner. That runner is also the
client-local location in the table above. Use direct `bazelisk test` for a
non-Docker live target that needs credentials or services from the actual host.
`bazelisk run` and `bb run` build through RBE but execute the requested binary
on the caller.

Local cache layout — including why the shared host cache deliberately does not
share `--repo_contents_cache` across output bases — is owned by the User layer;
see <bazel_worktree_cache_sharing.md>.

## Verification

`devinfra/nixos_bazel_test/run.sh test` is the NixOS end-to-end contract. It
boots the real `bazel-test` image and seeds a miniature workspace with the
production `rbe.bazelrc`. The shared smoke script also runs directly on the FHS
GitHub runner. On both clients it proves from execution logs that:

- Ruff and all `bazel run` build actions execute remotely before the completed
  binary runs in the client;
- an ordinary `TestRunner` and its build action execute remotely; and
- an explicitly local `TestRunner` runs in the client while its build action
  still executes remotely.

`.github/workflows/nixos-bazel-rbe.yml` runs both FHS and NixOS variants for
relevant internal pull requests, pushes to `devel`, and manual dispatches.
GitHub cannot provide the SOPS decryption secret to fork pull requests, so those
runs are skipped.

For cache layout and worktree mechanics, see
<bazel_worktree_cache_sharing.md>. For `bb remote`'s outer-runner behavior, see
<bb_remote_internals.md>. Historical NixOS compatibility investigation is in
<../../debug/nixos_bazel_bash/README.md>.
