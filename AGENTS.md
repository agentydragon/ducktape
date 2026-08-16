@README.md

## Target Platform

Linux only.

## Nix Devshell / Missing Tools

All tools (`bazelisk`, `bbr`, `pre-commit`, `ducktape-precommit`, `rustfmt`, the
repo's pinned `prettier` + plugins, etc.) come from the Nix devshell, not global
installs. If any are missing or not on `PATH`, the devshell isn't loaded:

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
It uses an in-memory kubeconfig and never triggers permission prompts.

**RBAC source of truth**: keep permission details in
<cluster/k8s/agents/agent-rbac-base/README.md> and the RoleBinding files it points to,
not in this root agent file. Check those docs before assuming namespace coverage
or write permissions.

**Escape hatch**: `Bash(kubectl ...)` uses the user's personal kubeconfig (CLI) or
session kubeconfig (web) for operations needing higher privileges or other namespaces.

In-cluster OAuth MCP variants are documented in
<cluster/docs/mcp_oauth_authentik_notes.md>.

## Sandbox

Run `bb`, `bazel`, `bazelisk`, `bbr`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`, `ip`, `curl`, and other network/system commands **outside the sandbox** (`dangerouslyDisableSandbox: true`). The sandbox blocks their network calls (including localhost, e.g., `kubectl` to haproxy on `localhost:7445`).

For the Bazel family (`bazel`, `bazelisk`, `bb`, `bbr`) this is unconditional, with no in-sandbox fallback to try first: when any `WebFetch(domain:...)` permission rule exists in settings, the sandbox applies `--unshare-net` (full network namespace isolation), which breaks Bazel's gRPC DNS resolution even for allowlisted hosts — BuildBuddy/RBE/BES traffic has no supported path through it. <docs/claude_code_sandbox.md> is the operational rule; <devinfra/docs/bazel_worktree_cache_sharing.md> covers local CLI cache and proxy shims and does not override it. <debug/bazel_sandbox_mitigations.md> is the historical March 2026 investigation only.

## Bazel Commands

**Prefer `bbr` for build/test/query.** Use `bb run` for targets whose binary must
execute locally (Gazelle, manifest updates, formatters). Use `bazelisk` for local
runs that need the session bazelrc. Avoid raw `bazel`.

```bash
bbr test //path/to:target
bbr build //path/to:target
bbr query '...'

# Runs locally (binary always executes on the local machine):
bb run //devinfra:gazelle
```

Remote execution (RBE) and remote caching are the **expected defaults** — do not disable them. No workflow needs `--remote_executor=""` — see <devinfra/docs/rbe_workflows.md> for the per-workflow reasoning (gazelle, requirements, cargo repin, pnpm, syrupy). In particular:

- **Browser/visual tests must run with remote execution.** RBE runner VMs have the required Docker and display stack; local machines typically do not. Never skip or stub these tests to avoid needing RBE.
- `--noremote_cache` / `--noremote_accept_cached` are fine for forcing a fresh run; they don't break correctness.

**If a Bazel-family command cannot reach BuildBuddy** (connection refused, DNS failure, cert error) while already running outside the sandbox, **stop and report the connectivity issue to the user.** They may need to recover the session start hook or check VPN/firewall state.

Build outputs, invocation data, and `bbr` configuration layers live in
<devinfra/bbr.py>. BuildBuddy log retrieval and target-history recipes live in the
`buildbuddy_api` skill.

### NixOS hosts

Ducktape build-like commands use strict BuildBuddy RBE; NixOS local and remote-worker shell paths are configured separately. See <devinfra/docs/bazel_configuration.md> for the rc ownership and execution contract.

## Terraform via Bazel

Terraform/OpenTofu modules are managed by Bazel (`@rules_tf`). Each module has a
`BUILD.bazel` with `tf_providers_versions` and `tf_module` rules. **There are no
hand-written `terraform.tf` or `required_providers` blocks** — Bazel generates them
from `tf_providers_versions`. Do not suggest adding `terraform { required_providers }`
blocks manually.

## Haku Forgejo Tokens

Forgejo tokens and credentials consumed by `haku-ci` or Haku pods must be produced by
the in-cluster GitOps Terraform controller, not minted, copied, or synchronized
manually. Change the Terraform/GitOps wiring under <tf/gitops/haku-state> and the Flux
wrapper under <cluster/k8s/forgejo/haku-state> so reconcile creates and repairs the
Kubernetes Secrets and Forgejo Actions secrets. If live token state is stale, fix that
Terraform/controller wiring and reconcile it; do not repair drift by updating live
Forgejo repo Actions secrets or Kubernetes Secrets directly. Manual `curl`, `tea`, or
`kubectl` edits are only incident diagnostics; follow them with a PR that makes the
controller own the state.

## Flux Kustomization Wiring

Flux `Kustomization` resources (`flux-kustomization.yaml`) are applied from the **root**
`cluster/k8s/kustomization.yaml`, not from local `kustomization.yaml` files in each
directory. A directory's `kustomization.yaml` should only list the manifests that Flux
applies at `spec.path` (e.g., `terraform.yaml`, `*.sops.yaml`). **Do not include
`flux-kustomization.yaml` in local `kustomization.yaml` resources** — it causes
redundant application.

Adding or publishing a container image (Bazel `oci_image`, GHCR push matrix, Flux image
automation, tag policy): see <cluster/docs/container-images.md>.

## CI Configuration

All CI runs through GitHub Actions → `bbr` (BuildBuddy RBE). No separate
`buildbuddy.yaml`. `.github/workflows/ci.yml` orchestrates the dependency chain
(rbe-image → bazel-ci → release/push-images/props-images). Independent workflows
(pre-commit, ansible-lint, nix-attic-push, openclaw-image) have their own triggers.

## Refactoring

When renaming/moving/deleting files or symbols, search **all references** across the entire codebase (imports, BUILD files, CI configs, docs, Dockerfiles, k8s manifests). Missing a reference is worse than being thorough.

**Atomic API changes**: update all callers in the same commit. No transitional shims within this monorepo.

### Declarative configuration scope

Before enabling, autostarting, or installing a declaratively managed feature,
search the entire configuration tree for existing ownership and overlapping
providers (for example, GNOME built-ins, tray applets, system packages, and Home
Manager modules). Keep one clear owner unless the duplication is intentional
and documented.

When a setting could apply to a class of hosts or only selected hosts, make the
scope explicit: use a shared module/profile for genuine class-wide behavior and
host-level opt-in for exceptions. Do not infer that a feature belongs everywhere
merely because it works on one host. If the intended scope is unclear, ask the
user before broadening it.

## Profiling

Use real profilers for performance investigations: `perf`, Callgrind
(`valgrind --tool=callgrind`), heaptrack, Massif, or the component's
Bazel profile targets. Do not commit ad hoc timing macros, fine-grained
phase timers, or one-off elapsed-time counters to production code just
to understand a hotspot. If temporary instrumentation is unavoidable,
keep it local to the investigation and remove it before review.

## Before Hand-off

**Default: open the PR and let CI run the tests.** The required checks already
run the affected Bazel targets on the PR, so a pre-push `bbr test` of the same
targets is the same run done twice — and the second one only delays review.
Push, watch the checks, fix what they report. Local `bbr` is for iterating on a
failure CI already found, for a change too uncertain to hand to CI as-is, and
for anything CI does not cover — it is not a gate you must clear first.

For changes that will **not** pass through the required PR checks (anything
landing without a PR), run the relevant validation before hand-off.

**Gotcha: `bbr test` on a test target does not lint the libraries it depends
on.** The mypy/ruff aspects only produce their output groups for the targets
_named on the command line_. On #4116 all 54 tests passed locally and CI still
failed — on `mypy //haku/console/x:claude_chat`, a library the local
`bbr build` had never named. So when you do run Bazel locally, name the changed
`py_library` targets too:

```bash
bbr test //pkg:test_foo
bbr build //pkg:foo   # gates the mypy aspect on the library itself
```

Changing a file's imports also changes its `deps`, which no local check covers
if `bb run //devinfra:gazelle` is unavailable — build the affected library to
catch a missing `@pypi//...` before CI does.

If you touched `ansible/`, also follow <ansible/AGENTS.md>.

### Watching CI on a PR

- `pull_request_read` with `method: get_check_runs` lists the check runs on the
  PR's head commit. **Trap:** the listing paginates and `Pre-commit checks`
  lands on a later page — a green first page is not a green PR. Page through
  (`page: 2`, …) before calling it clean.
- For a failure, `get_job_logs` with `run_id` + `failed_only: true` +
  `return_content: true` returns every failing job's log tail in one call.
- `Bazel CI` failures point at a BuildBuddy invocation; log-retrieval and
  target-history recipes are in the `buildbuddy_api` skill.

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

### Committing

Commit messages carry no test-invocation trailer: nothing parses one and no
`commit-msg` hook asks for one. `git log` is full of older commits ending in
`Bazel-Test-Invocations:` — don't copy the pattern. To point a reviewer at a
BuildBuddy run, link it in the PR body, where a human will click it.

One commit gotcha: **committing on `devel`** (the default branch) trips the
`no-commit-to-branch` hook. When intentionally committing there, skip only that
hook: `SKIP=no-commit-to-branch git commit …`.

## Conventions

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

**Docker tests run on RBE, never locally**: Docker tests (container E2E, mitmproxy
testcontainers) target RBE workers, which have Docker. Missing Docker locally is never a
reason to skip, stub, or declare them unrunnable — if RBE is unreachable, recover it or
abort and report. Use the `py_test` macro from `//devinfra/python:defs.bzl` (not raw
`@rules_python`) with `requires_docker = True`; it handles `env_inherit`, tags, and
Docker exec properties.

**Use undeclared test outputs for log capture**: write diagnostics (container logs, HAR
dumps, config snapshots) via `util.testing.undeclared_outputs.undeclared_outputs_dir()`,
not test stdout/stderr. They upload to BuildBuddy and download to
`bazel-testlogs/<target>/test.outputs/`.

**Test timeouts mean hangs, not slowness**: a timeout means something is wedged
(deadlock, container never ready, port nothing listens on) — do NOT bump
`size`/`timeout`. Trace the blockage: `--test_output=streamed --test_arg=-s`, fixture
logging, `docker ps`.

**Localizing test failures**: `bbapi target history` gives the pass/fail timeline —
faster than `git bisect` since BuildBuddy already has the results. Recipes:
`/buildbuddy_api` skill.

**Snapshot tests (syrupy)**: set `uses_syrupy = True` on `py_test` and put the `.ambr`
files in `data`. Update/retrieval workflow (RBE and local):
<devinfra/docs/syrupy_snapshots.md>.

### Live OpenAI API Tests

Use `live_openai_py_test` from `//openai_utils/testing:testing.bzl`. Generates `.mock` and `.live` targets. CI excludes `.live` via `--test_tag_filters=-live_openai_api`.

```python
# test_foo.py
async def test_mock(mock_client): ...

@pytest.mark.live_openai_api
async def test_live(live_openai): ...
```

For non-Docker `.live` targets, build, lint, and typecheck actions stay on RBE
while only `TestRunner` executes on the Bazel client. Run those targets with
direct `bazelisk test` when they need credentials or services from the actual
host; `bbr` makes its FHS outer runner the Bazel client. Live tests declared
with `requires_docker = True` execute entirely on RBE like every Docker test.

## JavaScript / TypeScript

Uses `@aspect_rules_js`. **Do NOT run raw `pnpm install`** -- Bazel manages pnpm (pinned in `MODULE.bazel`).

Adding deps: add to `package.json`, run Bazel (first build updates lockfile and fails), run again, commit `pnpm-lock.yaml`.

**Never depend on bare `//:node_modules` from a BUILD file or `.bzl` macro.** It
links the entire npm workspace into the action, inflating RBE input manifests and
downloads while hiding undeclared JavaScript imports. Declare the exact package
targets instead (for example, `//:node_modules/react` or
`//:node_modules/vitest`). Put packages imported by a `ts_library`/`js_library`
in that library's `deps` so bundlers and test runners receive them transitively;
add runtime-only packages explicitly to the owning `js_binary`/`js_test` data when
the entrypoint imports them directly. Do not use the aggregate as a resolver
fallback for missing deps. When changing an existing use, verify with:

```bash
rg -n '"//:node_modules"' --glob 'BUILD*' --glob '*.bzl' .
```

See <props/frontend/AGENTS.md> for frontend conventions.

@STYLE.md
