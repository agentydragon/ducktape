@README.md

Linux only.

## Environment

- **Tools come from the Nix devshell** (`bazelisk`, `bbr`, `pre-commit`, pinned `prettier`, …). If one is missing, load it (`direnv allow; eval "$(direnv export bash)"` or `nix develop`) — never a per-tool workaround or skipped hook.
- **Claude Code Web:** certificate errors, `bazel: command not found`, or unresolvable `remote.buildbuddy.io` mean session setup failed — stop and recover via <devinfra/claude/claude_hook/docs/session_start_recovery.md>. Never `--noverify`/`SSL_VERIFY=false`; tell the user if recovery fails.
- **Agentplane MCP:** use a connected Action when one fits; discover tools and current Action policy rather than assuming access. RBAC: <cluster/docs/agent_rbac.md>; staging: <cluster/docs/agentplane_mcp_staging.md>. `Bash(kubectl …)` uses your own kubeconfig, whose privileges may differ.
- **Sandbox:** run `bb`/`bazel`/`bazelisk`/`bbr`, `terraform`/`tofu`, `kubectl`, `systemctl`, `ss`, `ip`, `curl` and other network/system commands with `dangerouslyDisableSandbox: true`. Bazel always, without trying in-sandbox first: with any `WebFetch(domain:…)` rule present, `--unshare-net` breaks its gRPC DNS. Details: <docs/claude_code_sandbox.md>.

## Bazel Commands

- `bbr` for build/test/query; `bb run` when the binary must execute locally (Gazelle, manifest updates, formatters); `bazelisk` for local runs needing the session bazelrc; never raw `bazel`.
- RBE and remote cache are the defaults — never disable them (`--remote_executor=""` is never needed; <devinfra/docs/rbe_workflows.md>). Browser/visual and Docker tests run **only** on RBE; never skip or stub them. `--noremote_cache`/`--noremote_accept_cached` are fine.
- Can't reach BuildBuddy from outside the sandbox → stop and report (usually a broken session hook or VPN).
- Refs: <devinfra/bbr.py> (config layers), `buildbuddy_api` skill (logs, target history), <devinfra/docs/bazel_configuration.md> (NixOS hosts).

## Infrastructure

- **Terraform/OpenTofu** modules are Bazel-managed (`@rules_tf`): declare provider versions in the BUILD's `tf_providers_versions`, never in `terraform.tf`. `tf/gitops/**` is applied by the in-cluster tofu-controller; `cluster/terraform/` by `bazel run //cluster:bootstrap`.
- **Haku Forgejo tokens** are owned by the GitOps Terraform controller — never mint/sync by hand. Fix wiring in <tf/gitops/haku-state> and <cluster/cdk8s/forgejo/gitops_modules.py>; manual `curl`/`tea`/`kubectl` is diagnostics only, followed by a PR making the controller own it.
- **CI:** GitHub Actions → `bbr`; no `buildbuddy.yaml`. `.github/workflows/ci.yml` runs bazel-ci → release/push-images; other workflows trigger independently. Container images: <cluster/docs/container-images.md>.
- **RBE image repins are costly** (the digest is in every action's cache key and the Firecracker snapshot key). A needed edit to <devinfra/rbe_container_image/Dockerfile> is fine — never keep a falsehood to dodge one — but don't widen `rbe-container-image.yml`'s trigger or add anything Bazel could supply. Dev/agent tooling goes in <devinfra/buildbuddy_remote_runner/image.nix>.

## SOPS

`.sops.yaml` matches repo-relative paths: write the file in place, then `sops -e -i`. Run inside the repo direnv (`.envrc` derives `SOPS_AGE_KEY` from `~/.ssh/id_ed25519`). That key may encrypt to cluster recipients yet fail to decrypt existing Flux secrets — check before editing ciphertext; otherwise use ESO or an operator-held identity.

## Issue labels

- `upstream-waiting`: the issue records an upstream bug blocking us until upstream fixes it. Remove when no longer true.
- `upstream`: the defect itself is in an external dependency/service (workaround or not). Not for upgrades, feature gaps, our integration bugs, or migrating to an upstream fix.

## Changes

- **Refactoring:** search all references (imports, BUILD, CI, docs, Dockerfiles, manifests). Update every caller in the same commit; no transitional shims.
- **Stored data:** schema changes are new migrations, never edits to landed ones. Never destroy unregenerable user-authored data (repos, notebooks, mailboxes). Roll-safety: <STYLE.md> § General.
- **Declarative config:** before enabling a managed feature, find existing owners/overlapping providers and keep one. Host scope is explicit (shared module for class-wide, host opt-in for exceptions); ask if unclear.

## Profiling

Real profilers only (`perf`, Callgrind, heaptrack, Massif, Bazel profile targets); never commit ad hoc timing code.

## Delegation and model selection

Applies wherever the harness exposes model/effort (Codex; Claude Code's Agent tool — `fork` inherits). Tier names are Codex's; in Claude Code apply the same principles to its own models, not a tier mapping.

- Optimize total cost of correctly completed work (incl. review, retries, integration, latency, operator attention).
- Default: GPT-6 Luna `xhigh` for bounded, verifiable work, including real implementation with a clear contract and check. Lower effort for extraction; Luna `max` for harder reasoning. Sol `medium`/`high` for diagnosis with competing explanations; Astra/strong parent for subtle invariants or costly-to-verify architecture — say why. Size, "debugging" or importance alone are not reasons.
- Before dispatch, define deliverable, constraints, context, checks, stopping condition; set model/effort explicitly. Ask for the artifact, exact check results, open concerns. Do trivial work directly.
- Review the diff, affected invariants and actual checks — worker tests may share its misconception. Don't redo the task or add reviewers routinely.
- Retry once for a localized failure; escalate misunderstanding or nonprogress, carrying forward patch, facts, failed approaches.
- Cost (AA 2026-09-24, index v4.3.2): Luna `max` / Sol `medium` / Astra `high` ≈ 1.6× / 6× / 41× Luna `xhigh` per task. More: <docs/agent_delegation/README.md>.

## Splitting Work Into PRs

Operator review is the scarce resource. Split into small, independently approvable PRs (one self-contained change each, per [Google small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)) and dispatch them in parallel.

- Conflicts are not dependencies — whoever lands second rebases. The only real dependency is content that can't be specified until an open question is answered.
- Never queue ready work behind a contested change. Stack on unmerged PRs (say so, name the commit to review).
- When stacking PRs on GitHub, use native GitHub Stacks and follow <devinfra/skills/github_stacks/SKILL.md>.
- Under review, split uncontested parts (including those called "uncontroversial" or commented around) into PRs that land first; debounce a few minutes for the rest of the round.
- State what a PR deliberately leaves out, and why.
- Parallel agents may collide; don't coordinate or narrow scope to avoid it.

## Before Hand-off

- Open the PR and let CI test; local `bbr` is for iterating on CI failures, uncertain changes, or what CI doesn't cover. Changes bypassing PR checks get validated first.
- **Gotcha:** mypy/ruff aspects only lint targets named on the command line — `bbr build` changed `py_library` targets too. If gazelle is unavailable, building the library catches missing `@pypi//…` deps.
- Touched `ansible/` → <ansible/AGENTS.md>.
- **Watching CI:** `pull_request_read` `get_check_runs` paginates — `Pre-commit checks` is on a later page. Failure logs: `get_job_logs` with `run_id`, `failed_only: true`, `return_content: true`. `Bazel CI` failures → `buildbuddy_api` skill.

## Git

- Never amend a pushed commit.
- Never `git reset --soft` onto a moved base; rebase, then squash against `$(git merge-base HEAD origin/devel)`.
- Committing on `devel` trips `no-commit-to-branch`; `SKIP=` it only with explicit user approval.
- **A dirty checkout is the user's.** Never switch branches or commit on top of it unasked; if the work doesn't build on that state, use a fresh worktree; ask if ambiguous.
- **Worktrees:** use the one you started in when suitable; else one harness worktree or `/tmp/ducktape-worktrees/YYYY-MM-DD-<slug>`. Each costs ~250 MB, and once Bazel runs there ~2 GB RAM + 5–10 GB output base — reuse a worktree you've built in rather than multiplying them. `bbr` avoids the local cost.
- **Gotcha:** parallel Bash calls share one cwd. Start every worktree command with `cd <worktree> &&`; never end a chain with a `cd` elsewhere.

## Conventions

- Investigation notes go in the owning project's `debug/` (<README.md#debug>), not comments or PR text.
- Update a component's `SPEC.md` when its contract changes (example: <devinfra/claude/claude_hook/SPEC.md>).

## Testing

- Always via Bazel (`bbr test`), never direct pytest.
- **Every `py_test` needs `pytest_bazel.main()`** under `if __name__ == "__main__":` (dep `@pypi//pytest_bazel`) — otherwise it exits 0 without running tests.
- pytest-asyncio auto mode is on; a test using a session-scoped async fixture (e.g. `playwright_driver`) needs `@pytest.mark.asyncio(loop_scope="session")` or browser startup can hang.
- Never skip for missing tools; they come from runfiles or the RBE image.
- Docker tests: `py_test` from `//devinfra/python:defs.bzl` with `requires_docker = True`; they run on RBE.
- Diagnostics (container logs, HAR dumps) go to `util.testing.undeclared_outputs.undeclared_outputs_dir()`, not stdout.
- **A timeout means a wedge, not slowness** — usually waiting on something that never arrives. Trace it (`--test_output=streamed --test_arg=-s`, fixture logs, `docker ps`); don't raise `size`/`timeout`. Raise size (default `small`, 60s) only with evidence the test was actively working: `bbapi target history` timings plus a local pass.
- Localize breakages with `bbapi target history` (`buildbuddy_api` skill), not bisect.
- Syrupy snapshots: `uses_syrupy = True`, `.ambr` in `data`; <devinfra/docs/syrupy_snapshots.md>.
- **Visual tests** exist to publish screenshots to the PR for review; one that captures the intended state and succeeds is valid without behavioral assertions. Add readiness checks so the capture isn't a loading state. All captures go through `deterministic_browser_context` + `VisualPage.capture()` (<util/testing/frontend_visual/README.md> § One way in; publication: <devinfra/pr_visuals/README.md>). Keep shared render-health checks; a pass doesn't mean the image is right — review the published images.
- **Live OpenAI tests:** `live_openai_py_test` (`//openai_utils/testing:testing.bzl`) makes `.mock`/`.live` targets; mark `@pytest.mark.live_openai_api` (CI excludes the tag). Run non-Docker `.live` targets with `bazelisk test` for host credentials.

## JavaScript / TypeScript

- `@aspect_rules_js`; never run `pnpm install`. New dep: add to `package.json`, build (updates lockfile, fails), build again, commit `pnpm-lock.yaml`.
- Never depend on bare `//:node_modules` in BUILD/`.bzl`; use exact `//:node_modules/<pkg>` targets (imports in `deps`, runtime-only in `data`). Check: `rg -n '"//:node_modules"' --glob 'BUILD*' --glob '*.bzl' .`
- Frontend conventions: <props/frontend/AGENTS.md>.

@STYLE.md
