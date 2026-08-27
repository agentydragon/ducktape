@README.md

## Target Platform

Linux only.

## Nix Devshell / Missing Tools

All tools (`bazelisk`, `bbr`, `pre-commit`, `rustfmt`, the repo's pinned `prettier` +
plugins, etc.) come from the Nix devshell. If one is missing from `PATH`, load the
devshell — never fall back to per-tool workarounds (standalone prettier, ruff via pip,
skipping hooks):

```bash
direnv allow   # if not yet allowed
eval "$(direnv export bash)"  # load into the current shell (or use nix develop)
```

## Session Start Hook (Claude Code Web)

Certificate errors, `bazel: command not found`, or unresolvable `remote.buildbuddy.io`
mean session setup failed: **stop and recover first** via
<devinfra/claude/claude_hook/docs/session_start_recovery.md>. Never bypass proxy/cert
errors with `--noverify` or `SSL_VERIFY=false`; notify the user if recovery fails.

## Kubernetes MCP Server (`haku-console`)

Prefer the `haku-console` MCP server's Kubernetes passthrough tools — they keep the
operator-linked authorization boundary. RBAC source of truth:
<cluster/k8s/agents/agent-rbac-base/README.md> — check it before assuming namespace
coverage or write permissions. Escape hatch: `Bash(kubectl ...)` uses the personal (CLI)
or session (web) kubeconfig for higher privileges. In-cluster OAuth MCP variants:
<cluster/docs/mcp_oauth_authentik_notes.md>.

## Sandbox

Run `bb`, `bazel`, `bazelisk`, `bbr`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`,
`ip`, `curl`, and other network/system commands **outside the sandbox**
(`dangerouslyDisableSandbox: true`) — it blocks their network calls, including localhost.

For the Bazel family this is unconditional, with no in-sandbox attempt first: with any
`WebFetch(domain:...)` permission rule present, the sandbox's `--unshare-net` breaks
Bazel's gRPC DNS resolution even for allowlisted hosts. <docs/claude_code_sandbox.md> is
the operational rule; <devinfra/docs/bazel_worktree_cache_sharing.md> covers cache/proxy
shims and does not override it (<debug/bazel_sandbox_mitigations.md> is historical).

## Bazel Commands

**Prefer `bbr` for build/test/query.** Use `bb run` for targets whose binary must
execute locally (Gazelle, manifest updates, formatters). Use `bazelisk` for local runs
that need the session bazelrc. Avoid raw `bazel`.

```bash
bbr test //path/to:target
bbr build //path/to:target
bbr query '...'
bb run //devinfra:gazelle   # binary executes locally
```

Remote execution (RBE) and remote caching are the **expected defaults** — do not
disable them; no workflow needs `--remote_executor=""` (per-workflow reasoning:
<devinfra/docs/rbe_workflows.md>). Browser/visual and Docker tests run **only** with
remote execution — RBE workers have the Docker and display stack; never skip or stub
them to avoid RBE. `--noremote_cache` / `--noremote_accept_cached` are fine for forcing
a fresh run.

**If a Bazel-family command cannot reach BuildBuddy** while already outside the sandbox,
stop and report the connectivity issue to the user (usually a broken session start hook
or VPN/firewall state).

`bbr` configuration layers live in <devinfra/bbr.py>; BuildBuddy log retrieval and
target-history recipes in the `buildbuddy_api` skill. NixOS hosts have a separate rc
ownership and execution contract: <devinfra/docs/bazel_configuration.md>.

## Terraform via Bazel

Terraform/OpenTofu modules are managed by Bazel (`@rules_tf`): each module's BUILD
declares `tf_providers_versions` + `tf_module`, validated against the Bazel-managed
provider mirror. Declare provider versions in `tf_providers_versions` — never hand-edit
a module's `terraform.tf`/`required_providers`, which mirrors that declaration.
Execution differs by root: `tf/gitops/**` is reconciled by the in-cluster
tofu-controller; the metal infra under `cluster/terraform/` is applied by
`bazel run //cluster:bootstrap`, not the controller.

## Haku Forgejo Tokens

Forgejo tokens consumed by `haku-ci` or Haku pods are produced by the in-cluster GitOps
Terraform controller — never minted or synchronized manually. Fix stale token state by
fixing the wiring under <tf/gitops/haku-state> and <cluster/k8s/forgejo/haku-state> and
reconciling; manual `curl`/`tea`/`kubectl` edits are incident diagnostics only, followed
by a PR that makes the controller own the state.

## CI Configuration

All CI runs through GitHub Actions → `bbr` (BuildBuddy RBE); no `buildbuddy.yaml`.
`.github/workflows/ci.yml` orchestrates rbe-image → bazel-ci →
release/push-images/props-images; pre-commit, ansible-lint, nix-attic-push, and
openclaw-image trigger independently. Adding or publishing a container image:
<cluster/docs/container-images.md>.

## Refactoring

When renaming/moving/deleting files or symbols, search **all references** across the
entire codebase (imports, BUILD files, CI configs, docs, Dockerfiles, k8s manifests).

**Atomic API changes**: update all callers in the same commit. No transitional shims
within this monorepo.

### Declarative configuration scope

Before enabling or installing a declaratively managed feature, search the configuration
tree for existing ownership and overlapping providers (GNOME built-ins, tray applets,
system packages, Home Manager modules); keep one clear owner. Make host scope explicit:
a shared module/profile for class-wide behavior, host-level opt-in for exceptions —
working on one host does not imply a feature belongs everywhere. Ask if the intended
scope is unclear.

## Profiling

Use real profilers (`perf`, Callgrind, heaptrack, Massif, the component's Bazel profile
targets). Do not commit ad hoc timing macros or one-off elapsed-time counters; keep any
temporary instrumentation local to the investigation and remove it before review.

## Splitting Work Into PRs

**The scarce resource is operator review, not machine time.** Split aggressively into
independently approvable PRs and dispatch them in parallel — a reviewer can approve
three of five separate PRs today and argue about the rest for as long as it takes.
[Google's small-CLs guidance](https://google.github.io/eng-practices/review/developer/small-cls.html)
applies: the unit is one self-contained change, split by change and never by file, with
a floor — not so small its implications can't be understood — and size alone is grounds
for a reviewer to bounce a PR.

- **A conflict is not a reason to wait.** Rebasing across PRs is cheap agent work;
  sequencing costs the operator a great deal. Dispatch the simple overlapping change
  anyway; whoever lands second rebases.
- **Never queue ready work behind one contested change.**
- **Split again under review.** A review round usually contests a handful of things,
  and the parts the operator calls fine — or comments around without touching — are
  implied approved. Split the uncontested, independently landable parts off to land
  first — as one PR or several, wherever the change-unit boundaries fall — leaving the
  contested parts on the original PR. The faster small pieces land, the fewer and
  simpler the cross-agent rebases, and partial wins get secured instead of waiting out
  the argument.
- **The only real dependency is unspecifiable content** — the work genuinely depends on
  an open question's answer. "It will conflict" / "touches the same file" / "tidier
  afterwards" are not dependencies.
- **Stack rather than block**: work building on an unmerged PR branches from it and says
  so in the body, naming the commit to review.
- **Say what a PR deliberately leaves out**, and why, so the split reads as a decision.
- **Parallel agents will collide, and that is fine** — don't have them coordinate or
  narrow scope to dodge each other.

## Before Hand-off

**Default: open the PR and let CI run the tests.** The required checks run the affected
targets anyway; a pre-push `bbr test` of the same targets only delays review. Local
`bbr` is for iterating on a failure CI found, changes too uncertain to hand to CI as-is,
and anything CI does not cover. Changes that bypass PR checks entirely get validated
before hand-off.

**Gotcha: `bbr test` on a test target does not lint the libraries it depends on** — the
mypy/ruff aspects fire only for targets named on the command line. Name changed
`py_library` targets too:

```bash
bbr test //pkg:test_foo
bbr build //pkg:foo   # gates the mypy aspect on the library itself
```

Changed imports also change `deps`; if `bb run //devinfra:gazelle` is unavailable, build
the affected library to catch a missing `@pypi//...` before CI does.

If you touched `ansible/`, also follow <ansible/AGENTS.md>.

### Watching CI on a PR

- `pull_request_read` with `method: get_check_runs` lists check runs on the head commit.
  **Trap:** the listing paginates and `Pre-commit checks` lands on a later page — page
  through before calling it clean.
- For a failure, `get_job_logs` with `run_id` + `failed_only: true` +
  `return_content: true` returns every failing job's log tail in one call.
- `Bazel CI` failures point at a BuildBuddy invocation — recipes in the `buildbuddy_api`
  skill.

## SOPS

`.sops.yaml` rules match by **path relative to repo root**, so encrypting a file outside
the repo fails with "no matching creation rules found". Write to the final in-repo
location, then `sops -e -i path/to/file.yaml`. Run sops from within repo direnv:
`.envrc` derives `SOPS_AGE_KEY` from `~/.ssh/id_ed25519`, which SOPS does not
auto-discover (it only finds `id_rsa`).

## Git

**NEVER amend a commit that has already been pushed.**

**NEVER `git reset --soft` onto a base branch that has moved on the remote** — it stages
every upstream commit landed since divergence as if it were yours. Rebase first, then
squash against `$(git merge-base HEAD origin/devel)`.

### Committing

Committing on `devel` trips the `no-commit-to-branch` hook. Skip it
(`SKIP=no-commit-to-branch git commit …`) only when the user has explicitly approved a
direct commit on `devel`.

**A dirty checkout you started in is not yours.** Uncommitted changes in the working
tree — especially in the user's own checkouts (`~/code/ducktape` on wyrm2, rugged, …),
where a dirty `devel` serves as their staging playground — usually mean the dirt is
theirs. Never switch branches there or commit on top of it unasked: judge whether the
request builds on that dirty state; if not, do the work in a fresh worktree and send a
PR from it; if genuinely ambiguous, ask.

## Conventions

- **`debug/`**: write investigation notes here, not in code comments or PR descriptions.
- **`SPEC.md`**: update when the component's high-level contract changes. No
  implementation details — those go in README.md or code. Example:
  <devinfra/claude/claude_hook/SPEC.md>.

## Testing

**Always use Bazel**, not direct pytest/python (`bbr test //path/to:test_target`).

**CRITICAL gotcha**: every `py_test` target MUST have a `pytest_bazel.main()` entry
point (deps: `@pypi//pytest_bazel`) — without it, Bazel runs the file as a script which
exits 0 without running tests.

```python
import pytest_bazel
# ... tests ...
if __name__ == "__main__":
    pytest_bazel.main()
```

**pytest-asyncio auto mode** is configured via `conftest.py` hooks — do NOT add
`@pytest.mark.asyncio` decorators.

**No test skips for missing tools**: let the test fail. Tools come from Bazel runfiles
or the RBE worker image.

**Docker tests**: use the `py_test` macro from `//devinfra/python:defs.bzl` (not raw
`@rules_python`) with `requires_docker = True` — it handles `env_inherit`, tags, and
Docker exec properties. They run on RBE (§ Bazel Commands); missing local Docker is
never a reason to skip or stub.

**Undeclared test outputs for log capture**: write diagnostics (container logs, HAR
dumps) via `util.testing.undeclared_outputs.undeclared_outputs_dir()`, not
stdout/stderr. They upload to BuildBuddy and download to
`bazel-testlogs/<target>/test.outputs/`.

**A timeout is not a request for more time.** It usually means the test stopped making
progress and is waiting for something that will never arrive — a gate signal the
sequencing never sends, a container that never comes up, a port nothing listens on. A
test that orchestrates several processes and pushes them through gates by hand fails
this way whenever the ordering is wrong, and it looks exactly like slowness at the
moment the clock runs out. Raising `size`/`timeout` there buys the same wedge at the new
duration, and the next run spends all of it before telling you anything. Trace the
blockage: `--test_output=streamed --test_arg=-s`, fixture logging, `docker ps`.

**Sizes are still meant to match the work.** A test that genuinely spends minutes on
bounded work belongs at `medium` or `large`; a `py_test` here defaults to
`size = "small"` (60s, <devinfra/python/defs.bzl>). So raise one only on evidence that
the test was **actively working** when the clock ran out and would have finished shortly
— `bbapi target history` timings across commits, plus a local pass on the same tree.
"It timed out, so it needs longer" is the reasoning this rule exists to stop.

**Localizing test failures**: `bbapi target history` gives the pass/fail timeline —
faster than `git bisect`. Recipes: `buildbuddy_api` skill.

**Snapshot tests (syrupy)**: `uses_syrupy = True` on `py_test`, `.ambr` files in
`data`. Update/retrieval workflow: <devinfra/docs/syrupy_snapshots.md>.

### Live OpenAI API Tests

Use `live_openai_py_test` from `//openai_utils/testing:testing.bzl` — generates `.mock`
and `.live` targets; CI excludes `.live` via `--test_tag_filters=-live_openai_api`. Mark
live tests `@pytest.mark.live_openai_api`. Run non-Docker `.live` targets with direct
`bazelisk test` when they need host credentials/services (`bbr` makes its FHS outer
runner the Bazel client); `requires_docker = True` live tests execute entirely on RBE.

## JavaScript / TypeScript

Uses `@aspect_rules_js`. **Do NOT run raw `pnpm install`** — Bazel manages pnpm (pinned
in `MODULE.bazel`). Adding deps: add to `package.json`, run Bazel (first build updates
the lockfile and fails), run again, commit `pnpm-lock.yaml`.

**Never depend on bare `//:node_modules` from a BUILD file or `.bzl` macro** — it links
the whole npm workspace into the action and hides undeclared imports. Declare exact
package targets (`//:node_modules/react`); packages a `ts_library`/`js_library` imports
go in its `deps`, runtime-only packages in the owning `js_binary`/`js_test` `data`.
Verify when changing an existing use:

```bash
rg -n '"//:node_modules"' --glob 'BUILD*' --glob '*.bzl' .
```

See <props/frontend/AGENTS.md> for frontend conventions.

@STYLE.md
