@README.md

## Target Platform

Linux by default. macOS-only components (Seatbelt, Sandboxer) are explicitly documented.

@STYLE.md

## Session Start Hook (Claude Code Web)

If you see certificate errors, `bazel: command not found`, `Unable to resolve host
remote.buildbuddy.io`, or other signs that session setup failed: **stop and recover before
doing any other work.** Follow <devinfra/claude/hook_daemon/docs/session_start_recovery.md> completely.
Do not bypass proxy/certificate errors with `--noverify`, `SSL_VERIFY=false`, or similar.
The root cause is always a broken session start hook — notify the user if recovery fails.

## Kubernetes MCP Server (`claude-sandbox-kubectl`)

Prefer the `claude-sandbox-kubectl` MCP server tools over `Bash(kubectl ...)` for
`claude-sandbox` namespace operations. The MCP server uses the `claude-code-web`
ServiceAccount and never triggers permission prompts.

**RBAC** (see <cluster/k8s/agents/claude-rbac/>):

- **claude-sandbox namespace**: full CRUD on pods, pods/log, pods/exec, pods/attach,
  services, configmaps, secrets, PVCs, events, deployments, statefulsets, daemonsets,
  replicasets, jobs, cronjobs (<role-sandbox.yaml>). Quota: 8 CPU, 16Gi memory, 20 pods.
- **Cluster-wide read** (`cluster-diagnostics-reader` ClusterRole): nodes, pods,
  deployments, Flux kustomizations (+ patch for reconcile triggers), HelmReleases,
  cert-manager, CNPG clusters, metrics, Longhorn, Gateway API, Kyverno, and more.
- **Cross-namespace read**: harbor, langfuse, ollama, openclaw, props, gatus,
  logs/configmaps via namespaced rolebindings.

**Escape hatch**: `Bash(kubectl ...)` uses the user's personal kubeconfig (CLI) or
session kubeconfig (web) for operations needing higher privileges or other namespaces.

## Sandbox

Run `bb`, `bazel`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`, `ip`, `curl`, and other network/system commands **outside the sandbox** (`dangerouslyDisableSandbox: true`). The sandbox blocks their network calls (including localhost, e.g., `kubectl` to haproxy on `localhost:7445`).

## Bazel Commands

Use `bbr` for build, test, and query. Use `bb` directly only for `run`
(local side effects) or when you need outputs on the local filesystem.

```bash
bbr test //path/to:target
bbr build //path/to:target
bbr query '...'

# Local side effects only:
bb run --remote_executor="" //devinfra:gazelle
```

**Updating `requirements_bazel.txt`**: `bb run --remote_executor="" //:requirements.update`
fails on NixOS (no `/bin/bash`). Use RBE instead:

```bash
bbr build //:requirements --remote_download_regex='.*requirements\.out' --noremote_accept_cached
cp bb-out/bazel-out/k8-fastbuild/bin/requirements.out requirements_bazel.txt
# Then update the gazelle manifest:
bb run --remote_executor="" //devinfra:gazelle_python_manifest.update
```

`bbr` wraps `bb remote` (<devinfra/bbr.py>) — runs Bazel on a
BuildBuddy runner VM with RBE, Firecracker isolation, and Docker. Syncs local
git diffs automatically. See <devinfra/docs/bb_remote_internals.md>.

**Unpushed commits on default branch**: `bbr` aborts if local
`devel` differs from `origin/devel`. Fix: `git push` first, or use a feature
branch.

**Downloading build outputs from RBE**: `--config=rbe` sets `--remote_download_minimal`,
so build artifacts aren't downloaded by default. To force-download specific outputs,
add `--remote_download_regex='<java regex>'` (e.g., `--remote_download_regex='.*\.whl$'`)
or `--remote_download_outputs=toplevel` to fetch just the direct outputs of the
requested targets. This is additive with `--remote_download_minimal`. For `bbr`, pass
the flag before the target: `bbr build //target --remote_download_regex='.*\.whl$'`.

**Downloaded artifacts land at `bb-out/bazel-out/<config>/bin/<pkg>/<name>`**, not
`bb-out/bazel-bin/<pkg>/<name>`. The `bazel-bin/` convenience symlink only exists in
local Bazel workspaces — `bb remote` does not create it on the runner side. For our
standard RBE build (`--config=rbe --config=ci` from `.github/actions/bb-remote/`) the
config is `k8-fastbuild`, so outputs land at `bb-out/bazel-out/k8-fastbuild/bin/...`.
Workflows consuming bb-remote-built artifacts on the runner side (e.g. `push-images.yml`)
must use the full path. See <devinfra/docs/bb_remote_internals.md> for details.

**Requirements:** `bb` on PATH and `BUILDBUDDY_API_KEY` set (both provided by session
start hook).

**Configuration layers** (in priority order, last-wins for Bazel flags):

| Layer   | Source                           | Contents                                                  |
| ------- | -------------------------------- | --------------------------------------------------------- |
| Repo    | `devinfra/bbr.json` (checked in) | `runner_exec_properties`, `container_image`, `bazel_args` |
| Session | `$BBR_BAZELRC` file              | `--build_metadata` (ROLE, session TAGS)                   |
| Ad-hoc  | `$BBR_REMOTE_ARGS` env var       | Extra `bb remote` flags (slot 2)                          |

The session hook writes `bbr.bazelrc` and exports `BBR_BAZELRC` automatically.

**Invocation tracking:** `bbr` automatically writes the BuildBuddy invocation ID to
`~/.cache/bbr/last_invocation_id` and prints a post-run summary with `bbapi` commands:

```bash
# After any bbr command, use the printed invocation ID:
bbapi target <id>                    # List targets (auto-resolves workflow IDs)
bbapi target log <id> <target>       # Fetch test log
bbapi artifact <id>                  # List/download undeclared test outputs
bbapi invocation <id>                # Invocation details (commit, branch, dirty)

# Or read the last invocation ID from file:
cat ~/.cache/bbr/last_invocation_id

# List invocations filtered by session tag:
bbapi invocation list --tag session:<session-id>

# Target history (pass/fail timeline, useful for bisecting):
bbapi target history --label //path:target
```

`bbapi target` and `bbapi target log` auto-resolve workflow (runner) invocation IDs
to child invocations — either ID works.

## Terraform via Bazel

Terraform/OpenTofu modules are managed by Bazel (`@rules_tf`). Each module has a
`BUILD.bazel` with `tf_providers_versions` and `tf_module` rules. **There are no
hand-written `terraform.tf` or `required_providers` blocks** — Bazel generates them
from `tf_providers_versions`. Do not suggest adding `terraform { required_providers }`
blocks manually.

## Flux Kustomization Wiring

Flux `Kustomization` resources (`flux-kustomization.yaml`) are applied from the **root**
`cluster/k8s/kustomization.yaml`, not from local `kustomization.yaml` files in each
directory. A directory's `kustomization.yaml` should only list the manifests that Flux
applies at `spec.path` (e.g., `terraform.yaml`, `*.sops.yaml`). **Do not include
`flux-kustomization.yaml` in local `kustomization.yaml` resources** — it causes
redundant application.

## Container Images

Most container images are built with Bazel (`rules_oci`, `rules_distroless`). The
`.github/workflows/push-images.yml` matrix builds each `oci_image` on RBE, downloads
the layout to the GHA runner, and pushes via `crane`. Pushes are content-deduped: the
matrix first fetches the `<name>.digest` sibling target (auto-generated by `oci_image`)
and skips the push when the local digest already matches the newest `devel-*` tag in
the registry — so unchanged images don't roll Flux deployments. A few images use
Dockerfiles with GitHub Actions workflows (RBE worker, devbot, claude web_env). New
images should use `oci_image` and a row in `push-images.yml`'s matrix. For in-cluster
images with Flux image automation, also add the `ImageRepository` to the GitHub
webhook receiver at `cluster/k8s/flux-webhook/github-webhook-receiver.yaml` so new
tags are picked up immediately instead of on the 5m poll interval. See
<cluster/docs/container-images.md> for the full checklist.

## CI Configuration

All CI runs through GitHub Actions → `bbr` (BuildBuddy RBE). No separate
`buildbuddy.yaml`. `.github/workflows/ci.yml` orchestrates the dependency chain
(rbe-image → bazel-ci → release/push-images/props-images). Independent workflows
(pre-commit, ansible-lint, nix-attic-push, openclaw-image) have their own triggers.

## Refactoring

When renaming/moving/deleting files or symbols, search **all references** across the entire codebase (imports, BUILD files, CI configs, docs, Dockerfiles, k8s manifests). Missing a reference is worse than being thorough.

**Atomic API changes**: update all callers in the same commit. No transitional shims within this monorepo.

## Before Hand-off

```bash
bbr build //...
bbr test //...
```

Lint (ruff + mypy) runs by default. Use `--config=nolint` to skip.
If you touched `ansible/`, also follow <ansible/AGENTS.md>.

## SOPS

SOPS `.sops.yaml` creation rules match files by **path relative to the repo root**.
Running `sops -e /tmp/some-file.yaml` fails with "no matching creation rules found"
because the path doesn't match any rule. Always write the file to its final destination
(or a temp path under the repo) and encrypt in-place:

```bash
# Correct: write file to destination first, then encrypt in-place
cp /tmp/plaintext.yaml secrets/shared/kubeconfig.yaml
sops -e -i secrets/shared/kubeconfig.yaml

# Wrong: file outside repo — no creation rule matches
sops -e /tmp/plaintext.yaml > secrets/shared/kubeconfig.yaml
```

SOPS 3.12 auto-discovers `~/.ssh/id_rsa` but NOT `~/.ssh/id_ed25519`. The root
`.envrc` (via `devinfra/secrets/cli_env.sh`) sets `SOPS_AGE_KEY` automatically
by deriving from `~/.ssh/id_ed25519`. **Always run sops commands from within the
repo directory** (with direnv active) so `SOPS_AGE_KEY` is set correctly.
If you need to run sops outside the repo, derive the key manually:

```bash
export SOPS_AGE_KEY=$(ssh-to-age --private-key -i ~/.ssh/id_ed25519)
sops -d secrets/shared/kubeconfig.yaml
```

## Git

**NEVER amend a commit that has already been pushed.**

**NEVER use `git reset --soft` to squash onto a base branch that has moved on the remote.** `git reset --soft origin/devel` collapses _all_ differences between HEAD and `origin/devel` into the staging area — including commits other people landed on devel since your branch diverged. The resulting "squashed" commit silently re-applies every upstream change as if it were yours. Use `git rebase origin/devel` first to rebase, then squash with `git reset --soft $(git merge-base HEAD origin/devel)` so only your branch's changes are staged.

## Conventions

### Debug Notes

`<subproject>/debug/<topic>.md`: for persistent investigation notes (RCAs, debug logs). Examples: `debug/spice_lag/README.md`, `debug/wyrm-oom/INVESTIGATION.md`. The `cluster/` subproject uses `cluster/docs/lessons_learned/` instead.

### Plans

`plans/`: for future work or work in progress. Once a plan is fully completed, remove it from `plans/` (delete, or squash into short tombstone/summary elsewhere).

### SPEC.md — High-level component specifications

`<subproject>/SPEC.md`: high-level, user-facing specification of what a
component guarantees to its users. An outside observer should be able to read
SPEC.md to understand what behaviors they can rely on, without having to read
the implementation. Example: <devinfra/claude/hook_daemon/SPEC.md> describes
what the Claude Code hook daemon provides to every session, and the
`/web_selfcheck` skill runs the acceptance tests derived from it.

SPEC.md files **must** be updated when the high-level requirements of the
thing they cover change — a new class of credential gets injected, a new
shim behavior is added, a new profile lands, a new promise is made to the
agent, etc.

SPEC.md files **must not** record low-level implementation details that an
outside observer would not notice. "Credentials are refreshed regularly by
the backend service" belongs in SPEC.md; "credentials live in
`<session_dir>/creds.json` and rotate every 300s via RPC to
`rotate.example.com`" does not — that belongs in README.md or in the code.

### TODO Tracking

Subprojects use `TODO.md` for persistent TODO tracking. TODOs local to a specific code location are fine as inline comments; cross-cutting or project-level TODOs belong in `TODO.md`.

## Testing

**Always use Bazel**, not direct pytest/python:

```bash
bbr test //path/to:test_target
bb run --remote_executor="" //path/to:binary_target
```

**CRITICAL gotcha**: All `py_test` targets MUST have a `pytest_bazel.main()` entry point. Without it, Bazel runs the file as a script which exits 0 without running tests. Add `@pypi//pytest_bazel` to deps.

```python
import pytest_bazel
# ... tests ...
if __name__ == "__main__":
    pytest_bazel.main()
```

**pytest-asyncio auto mode**: configured via `conftest.py` hooks. Do NOT add `@pytest.mark.asyncio` decorators.

**No test skips for missing tools**: let the test fail. Tools come from Bazel runfiles or the RBE worker image.

**Docker tests run on RBE, never locally**: Tests that use Docker (e.g., container E2E tests, proxy integration tests with mitmproxy testcontainers) are designed to run on BuildBuddy RBE workers, which have Docker available. **Never** skip these tests because Docker is unavailable locally, disable them, or claim they are "not runnable." They work on RBE — that is the intended execution environment. If RBE is not working, recover it by following the "Recovering from a Broken Session Start Hook" section above. Every environment in which agents operate will have BuildBuddy accessible, either automatically (session start hook) or through manual recovery. If you cannot restore BuildBuddy remote execution after following recovery steps, **abort and report the issue to the user** rather than working around it with `--remote_executor=""` or local-only execution for tests that assume RBE.

Use `py_test` macro from `//devinfra/python:defs.bzl` (not the raw `@rules_python` `py_test`) and set `requires_docker = True`. The macro handles `env_inherit`, tags, and Docker exec properties automatically. Do not add `env_inherit = ["DUCKTAPE_DOCKER_CLIENT_KEY"]` or `tags = ["requires_docker"]` manually.

**Use undeclared test outputs for log capture**: Write diagnostic data (container logs, HAR dumps, config snapshots) to Bazel's undeclared test outputs directory via `util.testing.undeclared_outputs.undeclared_outputs_dir()`. These are uploaded to BuildBuddy and retrievable from the invocation. Do not dump large log blobs into test stdout/stderr — they clutter the test log and are harder to navigate. To read undeclared outputs from a test run:

```bash
TEST_DIR=$(bb info bazel-testlogs)/path/to/test_target
ls "$TEST_DIR/test.outputs/"          # list undeclared output files
cat "$TEST_DIR/test.outputs/my.log"   # read a specific output
```

On RBE, the outputs are downloaded to local testlogs dir after test completes (Bazel fetches them automatically). The mitmproxy fixture saves `proxy.har` to undeclared outputs as an example.

**Test timeouts mean hangs, not slowness**: When a test times out, assume it is wedged — an internal operation is waiting on something that will never arrive (deadlock, stuck future, container that never becomes ready, connection to a port nothing is listening on). Do NOT bump `size`/`timeout` as a fix. Instead, trace the execution to find what is blocked: run with `--test_output=streamed --test_arg=-s`, add logging around fixture setup, check for stuck containers (`docker ps`), etc. A test that ran in 35s last week and now times out at 60s is not "slow" — something broke internally.

**Localizing test failures with target history**: When a test is failing and you need to find when it broke, use `bbapi target history` to see the pass/fail timeline, then narrow to a commit range. This is faster than `git bisect` because BuildBuddy already has the results:

```bash
# 1. Check recent history for the failing target
bbapi target history //path/to:test_target

# 2. Identify the transition point (last pass → first fail)
# 3. Use git log to find commits in that range
git log --oneline <last-pass-commit>..<first-fail-commit>

# 4. Read the test log from the first failing invocation
bbapi target log <failing-invocation-id> test_target
```

Use the `/buildbuddy_api` skill for more details on the `bbapi` CLI.

### Updating syrupy snapshots

Snapshot tests use syrupy (`.ambr` files in `__snapshots__/`). Set `uses_syrupy = True`
in the `py_test` macro — this wires `BazelAmberExtension` which copies updated `.ambr`
files to undeclared test outputs for RBE retrieval.

On failure, the test prints the exact update command.

**RBE** (preferred — works for Docker tests too):

```bash
bb test --config=rbe --remote_download_outputs=toplevel \
  //path/to:snapshot_test \
  --test_arg=--snapshot-update --nocache_test_results

# Copy updated snapshot from undeclared outputs back to source tree
cp bazel-testlogs/path/to/snapshot_test/test.outputs/snapshot_test.ambr \
   path/to/__snapshots__/snapshot_test.ambr
```

**Local** (simpler, no copy step — syrupy writes through runfiles symlinks):

```bash
bb test //path/to:snapshot_test \
  --test_arg=--snapshot-update --nocache_test_results \
  --remote_executor="" --config=nolint
```

Then commit the updated `.ambr` files. See <devinfra/docs/syrupy_snapshots.md> for details.

### Live OpenAI API Tests

Use `live_openai_py_test` from `//openai_utils/testing:testing.bzl`. Generates `.mock` and `.live` targets. CI excludes `.live` via `--test_tag_filters=-live_openai_api`.

```python
# test_foo.py
async def test_mock(mock_client): ...

@pytest.mark.live_openai_api
async def test_live(live_openai): ...
```

## JavaScript / TypeScript

Uses `@aspect_rules_js`. **Do NOT run raw `pnpm install`** -- Bazel manages pnpm (pinned in `MODULE.bazel`).

Adding deps: add to `package.json`, run Bazel (first build updates lockfile and fails), run again, commit `pnpm-lock.yaml`.

See <props/frontend/AGENTS.md> for frontend conventions.
