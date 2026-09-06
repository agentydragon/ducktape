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

## Execution environments and entry points

There are two different locality decisions:

1. **Where the Bazel client runs.** Bzlmod module extensions, repository rules,
   analysis, and Bazel-internal file work run here. This includes rules_js's
   pnpm/Node setup and any lockfile update performed during repository setup.
2. **Where build actions run.** Compilation, code generation, lint, typecheck,
   and ordinary test actions may run on BuildBuddy workers when `rbe` is
   enabled.

BuildBuddy remote execution does not move the Bazel client automatically. A
local `bb run` therefore still needs the caller's runtime to execute downloaded
repository-rule tools, even though its build actions are remote. Conversely,
`bbr`/`bb remote` moves the outer Bazel client to a BuildBuddy runner, so the
runner's environment—not the caller's—runs repository rules and the completed
`run` binary.

Use the entry points as follows:

| Entry point                       | Bazel client and repository rules | Build actions                                     | Completed `run` binary / local live test | Use it for                                                                    |
| --------------------------------- | --------------------------------- | ------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------- |
| `bb build` / `bb test` / `bb run` | Caller                            | BuildBuddy, under `rbe`                           | Caller                                   | Caller-local services, Kubernetes access, and live tests                      |
| `bbr` / `bb remote`               | BuildBuddy's outer FHS runner     | BuildBuddy, if the runner receives `--config=rbe` | Outer runner                             | Ordinary builds, tests, and queries from an uncontrolled or Nix-shaped client |
| Direct `bazelisk` on NixOS        | NixOS host                        | Local or BuildBuddy, explicitly selected          | NixOS host                               | Controlled development and debugging                                          |
| NixOS devbox/VM                   | Controlled NixOS host             | Local or BuildBuddy, explicitly selected          | VM                                       | A stable local client and caller-local acceptance tests                       |

`bbr` is the preferred default for ordinary build-like work from Claude Code
Web and similar uncontrolled containers. It avoids requiring those containers
to reproduce every NixOS/FHS compatibility detail. Use direct `bb run` (or
direct `bazelisk`) when the requested process must retain the caller's network,
Kubernetes credentials, filesystem, or other local services.

Do not interpret remote failure as permission to fall back locally. The RBE
configuration deliberately makes locality explicit so a missing remote
credential or unavailable worker cannot silently change the execution and
credential boundary.

### Credential boundary for hosted `bb remote`

The current egress credential-substitution contract is a **local-client**
contract. It can replace a whole HTTP header or gRPC metadata value while the
request crosses the Agentplane proxy. It does not give a BuildBuddy-hosted
runner the caller's Sandbox workload identity.

With direct `bb run`, the local client and any caller-local live test remain in
the Sandbox, so a configured placeholder can be resolved on the intended local
path. With `bbr`/`bb remote`, the outer control request may be authenticated,
but the Bazel command embedded in `RunRequest` is executed later on the
BuildBuddy runner. Under the current header-only substitution contract, that
embedded placeholder remains inert. The hosted runner also has neither the
caller Pod's Kubernetes token nor a runner-side Agentplane gateway, so it
cannot use the current Sandbox-authenticated path to reach the staging
Kubernetes API.

Therefore:

- use direct `bb run` from a controlled caller for staging acceptance tests that
  need Kubernetes access;
- use `bbr` for ordinary builds and tests that do not need caller-local
  services or credentials;
- do not treat successful outer `bb remote` authentication as proof that the
  nested hosted Bazel process can authenticate; and
- do not add a generic body rewrite or place a real staging credential in the
  hosted command as an incidental workaround.

The deferred hosted-runner alternatives and their weaker/stronger security
boundaries are recorded in
[`x/agentplane/plans/buildbuddy_remote_auth.md`](../../x/agentplane/plans/buildbuddy_remote_auth.md).

### NixOS and Nix-built images

NixOS hosts and Nix-built OCI images share package ownership but not a runtime
substrate. NixOS can use `nix-ld`, the system Bazel rc, and stable
`/run/current-system/sw/bin` paths. A `dockerTools` image has no NixOS
activation, `envfs`, or automatic `nix-ld` setup; downloaded FHS-linked tools
need actual filesystem defaults such as `/lib64/ld-linux-x86-64.so.2`, expected
library directories, `/bin/bash`, and `/usr/bin/env`.

The public-coder image should keep this compatibility surface in its Nix image
definition. A controlled NixOS devbox/VM should keep the host-specific pieces
in the NixOS module. Neither environment should leak Nix store paths into
remote actions; see the shell-path and strict-action-environment rules below.

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
