# Bazel configuration and BuildBuddy RBE

## Policy

Ducktape's [workspace rc](../../.bazelrc) imports the [RBE policy](../bazel/rbe.bazelrc)
for every build, test and run. Build actions, including code generation, lint and
typechecking, execute on BuildBuddy. An unavailable worker or missing credential fails the build.

| Layer      | Owner                                                                                                          | Settings                                                               |
| ---------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| System     | [NixOS Bazel module](../../nix/nixos/modules/bazel/system.bazelrc)                                             | Host shell, host/test `PATH`, `nix-ld`                                 |
| User       | Home Manager's `~/.bazelrc`                                                                                    | UI, [local caches](bazel_worktree_cache_sharing.md), credential import |
| Credential | [Home Manager](../../nix/home/modules/buildbuddy.nix) or [session/CI setup](../buildbuddy/docs/integration.md) | `common:rbe --remote_header=…`                                         |
| Workspace  | [RBE policy](../bazel/rbe.bazelrc)                                                                             | RBE selection, endpoints, action placement, worker shell               |

The credential applies only when a workspace enables `rbe`. Entry points may add metadata,
output preferences, filters and credentials. Keep execution policy in the workspace rc.

## Execution environments and entry points

| Command                              | Where the Bazel client runs | Use                                                                         |
| ------------------------------------ | --------------------------- | --------------------------------------------------------------------------- |
| `bbr` (`bb remote`)                  | BuildBuddy's hosted runner  | Ordinary build, test and query                                              |
| `bb run`                             | Caller                      | Run a built program against local files, services or Kubernetes credentials |
| `bazelisk test` on a controlled host | Caller                      | Non-Docker live tests needing host credentials                              |

Module extensions, repository rules (including pnpm and lockfile updates), analysis,
symlink creation and the completed `bazel run` program run on the **client**.
For local regeneration and retrieval, see [lockfiles](lockfiles.md) and
[Syrupy snapshot updates](syrupy_snapshots.md#updating-snapshots).

### Credential boundary for hosted `bb remote`

The hosted client cannot use the caller Pod's Kubernetes identity. An Agentplane egress
proxy can authenticate the outer request, but a placeholder in the nested Bazel command
remains a placeholder. Staging projects a BuildBuddy key for hosted builds. Run staging
Kubernetes checks from a controlled caller with the required access. Agent-container
execution policy, key exposure and deferred designs belong to the
[Agentplane authentication guide](../../agentplane/docs/buildbuddy_remote_auth.md).
See also [hosted runner configuration](../buildbuddy/docs/remote.md).

## Action placement

Normal and Docker tests run on RBE. The [live OpenAI test macro](../../openai_utils/testing/testing.bzl)
marks credentialed non-Docker tests for execution on the client. Bazel propagates
`no-remote-exec` to build and aspect actions too. The workspace strips it from every
mnemonic except `TestRunner`. The `remote,local` strategy can then select a local runner
for an explicitly local test. A remote failure cannot retry locally. CI builds live targets
for lint and typechecking and filters credentialed test runs.

## Shell paths and trust

- **NixOS host.** The system rc uses `/run/current-system/sw/bin/bash` and sets test
  `PATH` for client-local `#!/usr/bin/env bash` launchers. On RBE, the shell is
  `/bin/bash`. Bazel chooses `--shell_executable` for the configuration, before it
  places individual actions. Strict action environments keep host Nix store paths
  out of remote spawns. [NixOS compatibility investigation](../debug/nixos_bazel_bash/README.md)
- **Nix-built OCI client.** Repository tools downloaded for FHS need a loader
  (e.g. `/lib64/ld-linux-x86-64.so.2`), its library directories, `/bin/bash` and
  `/usr/bin/env` in the image. `dockerTools` images have no NixOS activation to supply them.
- **Host CA.** The [devshell entry point](../../.envrc) preserves a supplied
  `SSL_CERT_FILE` and otherwise uses `/etc/ssl/certs/ca-certificates.crt`.
  Keep the host choice when adding shell packages. The [Agentplane Pod](../../cluster/cdk8s/agentplane/sandbox_pod.py)
  supplies the proxy CA alongside the image roots, the JVM's PKCS12 store and
  proxy/CA variables for tests. Repository fetches use the client's trust configuration.

## Client job limit

`build:rbe --jobs=100` caps concurrent actions in the Bazel client. At 50, the Pod
and VM test actions waited for a slot: `command.profile.gz` showed 50 overlapping
`execute remotely` spans while the provider queue stayed near 0.06 s. On
2026-11-12, `//agentplane/...` with fresh tests (`--nocache_test_results`) took
602.7 s and 607.0 s at 50 and 447.4 s at 100. The Pod `TestRunner` action started
at 58.1 s at 100, and at 100.1 s and 212.7 s in the two runs at 50. That is one
sequence, not a general saving.

One pair of full `//...` sweeps on 2026-10-31, with fresh tests and warm compiled
actions: 100 took 30.7 s longer than 50 and hit `REMOTE_CACHE_EVICTED` and
`RST_STREAM` failures; concurrency was not shown to cause them.

## Verification

The [execution contract workflow](../../.github/workflows/nixos-bazel-rbe.yml)
runs the [same smoke checks](../nixos_bazel_test/smoke_test.sh) on the FHS GitHub runner
and a booted NixOS image (`devinfra/nixos_bazel_test/run.sh test`). Both use the production
RBE rc in a miniature workspace. Execution logs must show remote Ruff and build actions,
a remote regular test and a client-local tagged test with a remote build action. The
completed `bazel run` binary must produce its marker.

The workflow runs on relevant same-repository PRs, pushes to `devel` touching those paths
and manual dispatches. It uses `BUILDBUDDY_API_KEY` and `ATTIC_READ_TOKEN`.
