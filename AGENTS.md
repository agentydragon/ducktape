@README.md

## Target Platform

Linux by default. macOS-only components (Seatbelt, Sandboxer) are explicitly documented.

## Nix Devshell / Missing Tools

This repo has a Nix flake with a devshell and a `.envrc` that activates it via
direnv. On any machine with Nix installed, all tools (`bazelisk`, `bbr`,
`pre-commit`, `ducktape-precommit`, `rustfmt`, the repo's specific `prettier`
with its plugins, etc.) are provided by the devshell — not installed globally.

If any of these tools appear missing or not on `PATH`, the devshell is likely not
active. The fix is to ensure direnv has loaded the `.envrc`:

```bash
direnv allow   # if not yet allowed
eval "$(direnv export bash)"  # to load it in the current shell
```

Or just run the operation under `nix develop` directly if direnv is unavailable.

Do **not** resort to per-tool workarounds (downloading a standalone prettier,
invoking ruff via pip, skipping hooks, etc.) — the Nix devshell provides exactly
the right versions and plugin configurations.

## Session Start Hook (Claude Code Web)

If you see certificate errors, `bazel: command not found`, `Unable to resolve host
remote.buildbuddy.io`, or other signs that session setup failed: **stop and recover before
doing any other work.** Follow <devinfra/claude/claude_hook/docs/session_start_recovery.md> completely.
Do not bypass proxy/certificate errors with `--noverify`, `SSL_VERIFY=false`, or similar.
The root cause is always a broken session start hook — notify the user if recovery fails.

## Kubernetes MCP Server (`kubectl-local`)

Prefer the `kubectl-local` MCP server tools over `Bash(kubectl ...)` for any
operation that the `oidc-ksbx-groups:kubectl-sandbox-users` RBAC group allows.
The MCP server uses a client certificate (CN=`claude-code-web`, group
`oidc-ksbx-groups:kubectl-sandbox-users`) and never triggers permission prompts.

**RBAC** (see <cluster/k8s/agents/claude-rbac/>):

- **claude-sandbox namespace**: full CRUD on pods, pods/log, pods/exec, pods/attach,
  services, configmaps, secrets, PVCs, events, deployments, statefulsets, daemonsets,
  replicasets, jobs, cronjobs (<role-sandbox.yaml>); resource-quota-limited (see
  <cluster/k8s/agents/claude-rbac/README.md>).
- **Cluster-wide read** (`cluster-diagnostics-reader` ClusterRole): nodes, pods,
  deployments, Flux kustomizations (+ patch for reconcile triggers), HelmReleases,
  cert-manager, CNPG clusters, metrics, Longhorn, Gateway API, Kyverno, and more.
- **Cross-namespace read**: per-service `agent-rbac/` directories bind ClusterRoles
  in each target namespace. See `cluster/k8s/agents/claude-rbac/README.md` for
  context; the per-namespace breakdown is:

@cluster/k8s/agents/claude-rbac/permissions.md

**Escape hatch**: `Bash(kubectl ...)` uses the user's personal kubeconfig (CLI) or
session kubeconfig (web) for operations needing higher privileges or other namespaces.

**In-cluster MCP variants** (also available to any MCP client — Claude Code, claude.ai):

- `kubectl-sandbox-mcp` at `https://kubectl-sandbox-mcp.allegedly.works/mcp` —
  Authentik OAuth + custom scope mapping forces the issued token's `groups`
  claim to `["kubectl-sandbox-users"]` regardless of caller. Even an admin
  only gets sandbox access through this server.
- `kubectl-passthrough-mcp` at `https://kubectl-passthrough-mcp.allegedly.works/mcp` —
  Authentik OAuth + passthrough; caller's own OIDC group claims go to kube-apiserver
  (i.e., admin gets admin).

Both are public OAuth2 clients (PKCE, no client_secret). For design rationale,
DCR workaround, and the whole OAuth-dance saga, see
<cluster/docs/mcp_oauth_authentik_notes.md>.

## Sandbox

Run `bb`, `bazel`, `bazelisk`, `bbr`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`, `ip`, `curl`, and other network/system commands **outside the sandbox** (`dangerouslyDisableSandbox: true`). The sandbox blocks their network calls (including localhost, e.g., `kubectl` to haproxy on `localhost:7445`).

**All Bazel-family commands (`bazel`, `bazelisk`, `bb`, `bbr`) must always use `dangerouslyDisableSandbox: true`.** When any `WebFetch(domain:...)` permission rule exists in settings, the sandbox applies `--unshare-net` (full network namespace isolation). Bazel's Java gRPC client ignores `GRPC_PROXY` and performs direct DNS resolution — which is impossible in an isolated network namespace. This breaks RBE, BES upload, and remote cache. The sandbox proxy cannot fix this because Bazel's `--remote_proxy` / `--bes_proxy` only accept Unix sockets (raw TCP forwarders), not the HTTP CONNECT / SOCKS5 proxies the sandbox provides. See <docs/claude_code_sandbox.md> and <debug/bazel_sandbox_mitigations.md> for full analysis.

## Bazel Commands

### Tool hierarchy

| Tool       | What it is                                                                                                                                                       |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bazelisk` | Bazel version manager — downloads and runs the correct Bazel version. Our shim also injects the session bazelrc.                                                 |
| `bazel`    | Raw Bazel binary (avoid — version not pinned, no session bazelrc injection).                                                                                     |
| `bb`       | BuildBuddy CLI. `bb remote` runs Bazel **on BuildBuddy RBE runner VMs** using files synced from the local repo.                                                  |
| `bbr`      | Our convenience wrapper around `bb remote` (<devinfra/bbr.py>). Adds RBE flags, session tags, and auto-syncs git diffs. **Default choice for build/test/query.** |

**Prefer `bbr` for almost everything.** Use `bb run` for targets that need to execute on the local machine (Gazelle, manifest updates, formatters). Use `bazelisk` for local runs that need the session bazelrc (cert injection, new external repo fetches).

`bb run` always executes the binary locally — build actions use RBE by default.

```bash
bbr test //path/to:target
bbr build //path/to:target
bbr query '...'

# Runs locally (binary always executes on the local machine):
bb run //devinfra:gazelle
```

### Remote execution and caching

Remote execution (RBE) and remote caching are the **expected defaults** — do not disable them. In particular:

- **Browser/visual tests must run with remote execution.** RBE runner VMs have the required Docker and display stack; local machines typically do not. Never skip or stub these tests to avoid needing RBE.
- `--noremote_cache` / `--noremote_accept_cached` are fine for forcing a fresh run; they don't break correctness.

**If any Bazel-family command (`bazel`, `bazelisk`, `bb`, `bbr`) cannot reach BuildBuddy** (connection refused, DNS failure, cert error):

1. First, retry the Bash tool call with `dangerouslyDisableSandbox: true` — the Claude Code sandbox's `--unshare-net` breaks Bazel's gRPC DNS resolution even when the host is listed in the domain allowlist (see <docs/claude_code_sandbox.md>).
2. If it still fails, **stop and report the connectivity issue to the user**. The user may need to recover the session start hook or check VPN/firewall state.

### Downloading remote build outputs

`bbr` uses `--remote_download_minimal` by default, so artifacts stay on the runner. To fetch specific outputs:

```bash
# Fetch by regex (most common):
bbr build //path/to:target --remote_download_regex='.*\.whl$'

# Fetch all direct outputs of the requested targets:
bbr build //path/to:target --remote_download_outputs=toplevel
```

Artifacts land at `bb-out/bazel-out/k8-fastbuild/bin/<pkg>/<name>` (not `bazel-bin/` — that symlink only exists in local workspaces).

### Undeclared test outputs (golden PNGs, logs, HAR dumps)

Tests write diagnostics to Bazel's `TEST_UNDECLARED_OUTPUTS_DIR`. These are
uploaded to BuildBuddy automatically. Fetch them with `bbapi artifact`:

```bash
INV=$(cat ~/.cache/bbr/last_invocation_id)

# List what's available (shows LABEL and NAME columns):
bbapi artifact list "$INV"

# Stream a specific file to stdout (pipe or redirect):
bbapi artifact cat "$INV" pave_output.txt > local_copy.txt

# Download to a file (defaults to the artifact's own filename):
bbapi artifact download "$INV" pave_output.txt
```

`name-substr` is matched against `"label/name"` (e.g. `"test_render/test.outputs/empty.png"`).

To update golden screenshots after a visual test run:

```bash
INV=$(cat ~/.cache/bbr/last_invocation_id)
bbapi artifact download "$INV" "test.outputs/product_cash_runway.png"
cp product_cash_runway.png augur/frontend/__screenshots__/product_cash_runway.png
```

The outer (bbr) invocation ID auto-resolves to the child Bazel invocation — either ID works.

### Invocation tracking

`bbr` writes the BuildBuddy invocation ID to `~/.cache/bbr/last_invocation_id`:

```bash
bbapi target <id>                    # List targets (auto-resolves workflow IDs)
bbapi target log <id> <target>       # Fetch test log
bbapi artifact list <id>             # List undeclared test outputs
bbapi invocation <id>                # Invocation details (commit, branch, dirty)

bbapi invocation list --tag session:<session-id>   # All invocations in this session
bbapi target history --label //path:target          # Pass/fail timeline
```

### Session bazelrc and `bb` vs `bazelisk`

**`bb` ignores the session bazelrc; use `bazelisk` for local runs that need it.**
The Claude Code session start hook writes per-session Bazel config (JVM truststore,
RBE headers) to `<session_dir>/bazelrc`. The `bazelisk` shim auto-injects it; `bb`
does not. If `bb` starts a fresh Bazel server (option mismatch, idle timeout, new
external repo fetch) it will fail with `TLS error: PKIX path building failed`. For
local-only runs that need the session config, use `bazelisk run //target -- ...`.
RBE-bound work (`bbr ...`) is unaffected because runner VMs don't use the session
truststore.

### Updating `requirements_bazel.txt`

Use RBE to build, download the output, then regenerate the Gazelle manifest locally:

```bash
bbr build //:requirements --remote_download_regex='.*requirements\.out' --noremote_accept_cached
cp bb-out/bazel-out/k8-fastbuild/bin/requirements.out requirements_bazel.txt
bb run //devinfra:gazelle_python_manifest.update
```

### Unpushed commits

`bbr` aborts if local `devel` differs from `origin/devel`. Fix: `git push` first, or use a feature branch.

### Configuration layers

| Layer   | Source                           | Contents                                                  |
| ------- | -------------------------------- | --------------------------------------------------------- |
| Repo    | `devinfra/bbr.json` (checked in) | `runner_exec_properties`, `container_image`, `bazel_args` |
| Session | `$BBR_BAZELRC` file              | `--build_metadata` (ROLE, session TAGS)                   |
| Ad-hoc  | `$BBR_REMOTE_ARGS` env var       | Extra `bb remote` flags                                   |

**Requirements:** `bb` on PATH and `BUILDBUDDY_API_KEY` set (both provided by the session start hook or Nix devshell).

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

`.sops.yaml` rules match files by **path relative to repo root**.
`sops -e /tmp/some-file.yaml` fails with "no matching creation rules found"
because the path doesn't match any rule. Always write files to final locations
(or a temp path under the repo) and encrypt in-place:

```bash
# Correct: write to destination, then encrypt in-place
cp /tmp/plaintext.yaml secrets/shared/kubeconfig.yaml
sops -e -i secrets/shared/kubeconfig.yaml

# Wrong: outside repo — no creation rule matches
sops -e /tmp/plaintext.yaml > secrets/shared/kubeconfig.yaml
```

SOPS 3.12 auto-discovers `~/.ssh/id_rsa` but NOT `~/.ssh/id_ed25519`.
`.envrc` (via `devinfra/secrets/cli_env.sh`) sets `SOPS_AGE_KEY`
by deriving from `~/.ssh/id_ed25519`. **Always run sops commands from within
repo direnv** so `SOPS_AGE_KEY` is set correctly.
If you need to run sops outside the repo, derive the key manually (see `.envrc`).

## Git

**NEVER amend a commit that has already been pushed.**

**NEVER use `git reset --soft` to squash onto a base branch that has moved on the remote.** `git reset --soft origin/devel` collapses _all_ differences between HEAD and `origin/devel` into the staging area — including commits other people landed on devel since your branch diverged. The resulting "squashed" commit silently re-applies every upstream change as if it were yours. Use `git rebase origin/devel` first to rebase, then squash with `git reset --soft $(git merge-base HEAD origin/devel)` so only your branch's changes are staged.

## Conventions

See README.md for descriptions of each convention. Agent rules:

- **`x/`**: code under `x/` is experimental/unstable. Don't treat it as stable API.
- **`TODO.md`**: cross-cutting or project-level TODOs go here. Remove entries once done.
- **`plans/`**: delete or tombstone a plan once fully completed.
- **`debug/`**: write investigation notes here, not in code comments or PR descriptions.
- **`SPEC.md`**: update when the component's high-level contract changes (new promise, new credential class, new behavior visible to users). Do **not** record implementation details — those go in README.md or code. Example: <devinfra/claude/claude_hook/SPEC.md>.

## Testing

**Always use Bazel**, not direct pytest/python:

```bash
bbr test //path/to:test_target
bb run //path/to:binary_target
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

**Docker tests run on RBE, never locally**: Tests that use Docker (e.g., container E2E tests, proxy integration tests with mitmproxy testcontainers) are designed to run on BuildBuddy RBE workers, which have Docker available. **Never** skip these tests because Docker is unavailable locally, disable them, or claim they are "not runnable." They work on RBE — that is the intended execution environment. If RBE is not working, recover it by following the "Recovering from a Broken Session Start Hook" section above. Every environment in which agents operate will have BuildBuddy accessible, either automatically (session start hook) or through manual recovery. If you cannot restore BuildBuddy remote execution after following recovery steps, **abort and report the issue to the user** rather than working around it with local-only execution for tests that assume RBE.

Use `py_test` macro from `//devinfra/python:defs.bzl` (not the raw `@rules_python` `py_test`) and set `requires_docker = True`. The macro handles `env_inherit`, tags, and Docker exec properties automatically. Do not add `env_inherit = ["DUCKTAPE_DOCKER_CLIENT_KEY"]` or `tags = ["requires_docker"]` manually.

**Use undeclared test outputs for log capture**: Write diagnostic data (container logs, HAR dumps, config snapshots) to Bazel's undeclared test outputs directory via `util.testing.undeclared_outputs.undeclared_outputs_dir()`. These are uploaded to BuildBuddy and retrievable from the invocation. Do not dump large blobs into test stdout/stderr — they clutter the test log and are harder to navigate. To read undeclared outputs from a test run:

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
git log --oneline <last-pass>..<first-fail>

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

@STYLE.md
