# Branch protection for `agentydragon/ducktape`

## Goal

Enforce that commits merged to the default branch (`devel` now, `main` later)
have passed CI checks — specifically `Pre-commit checks` and `bazel-ci / Test &
Build`. The primary motivation is to prevent a red commit from silently landing
on `devel` and breaking downstream work, regardless of whether the commit came
from a PR merge, a direct push, or an automated workflow.

This is separate from (but related to) protecting against force-pushes and
branch deletion, which is a cheap extra we'd also pick up.

## Status: blocked, not started

- The first attempt (commit `c23edda`, PR #1312) was reverted in a follow-up PR
  because the Terraform apply couldn't succeed against a personal-account repo.
- No repository ruleset exists on the repo as of 2026-04-15.
- This doc captures what we tried, what we learned, and the options for a
  second attempt so we don't re-derive everything.

## What we tried in PR #1312

A tofu-controller-managed `github_repository_ruleset` at
`cluster/terraform/gitops/github-repo-rulesets/`, wired into Flux via
`cluster/k8s/github-repo-rulesets/`. The module was shaped like the existing
`github-secrets-sync`, `flux-webhook-token`, and `harbor-ci` modules (same k8s
backend, same PAT data source, same pattern).

The ruleset definition:

- Target: `refs/heads/devel`, `refs/heads/main` (one ruleset covering both so
  the protection survives an eventual default-branch rename with no gap).
- Rules: `deletion`, `non_fast_forward`, `required_linear_history`, `pull_request`
  (0 approvals required — it's a solo repo), `required_status_checks` with
  `Pre-commit checks` and `bazel-ci / Test & Build`.
- Bypass actors:
  - `RepositoryRole` actor_id=5 (admin): covers the owner account, and
    therefore any automation pushing with a PAT owned by the owner. Covers
    Flux `ImageUpdateAutomation` and the `claude-token-rotation` CronJob.
  - `Integration` actor_id=15368 (the `github-actions` built-in app):
    intended to cover the three GHA workflows that direct-push to `devel`
    via `GITHUB_TOKEN` — `sync-pins.yml`, `nix-flake-update.yml`, and the
    `pin-digests` job in `container-images.yml`.

## What went wrong

Two failures, in order:

1. **403 on apply.** The shared fine-grained PAT (`github-secrets-sync-pat`)
   didn't have `Administration: Read and write` repo permission, which the
   `POST /repos/{owner}/{repo}/rulesets` endpoint requires. Resolved by the
   owner adding the scope to the existing PAT.

2. **422 on apply.** With the right scope, the API then rejected the
   `Integration` bypass actor:

   > Actor GitHub Actions integration must be part of the ruleset source or
   > owner organization

   Verified from the GitHub web UI: on this personal-account repo, "GitHub
   Actions" simply does not appear in the bypass-actor picker at all. Other
   third-party apps installed on the repo (e.g. BuildBuddy) do appear. The
   built-in `github-actions` service isn't exposed as a bypass actor on
   personal-account repos via any mechanism we could find — neither the API
   nor the UI. This is a GitHub limitation for user-owned repos, not a bug in
   our Terraform or PAT scope.

## What this means for a second attempt

Without a GitHub Actions bypass, the three workflows that currently direct-push
to `devel` with `GITHUB_TOKEN` would start failing once the ruleset is active.
Those workflows are:

| Workflow                                   | Cadence       | What it pushes                  |
| ------------------------------------------ | ------------- | ------------------------------- |
| `sync-pins.yml`                            | every 30m     | `npins/sources.json` updates    |
| `nix-flake-update.yml`                     | (scheduled)   | `flake.lock` updates            |
| `container-images.yml` (`pin-digests` job) | on image push | `cluster/**` image digest bumps |

In-cluster automations that already push as the owner user via the PAT are
unaffected — they already bypass via `RepositoryRole=admin`:

- Flux `ImageUpdateAutomation` (uses `github-secrets-sync-pat`)
- `claude-token-rotation` CronJob (uses `github-secrets-sync-pat`)

So the open question is how to handle those three GHA workflows. Two paths,
both unappealing:

### Option A — PAT push

Sync `github-secrets-sync-pat` into a GitHub Actions secret (e.g.
`GH_ADMIN_PAT`) via the existing `github-secrets-sync` tofu module, then change
the three workflows to push using it. Commits become owner-attributed (bypass
via `RepositoryRole=admin`).

Pros: small workflow diff, in-cluster PAT rotation already handled.
Cons: the admin PAT now lives as a GHA secret, accessible to any workflow that
gets permission-creep. The owner's contribution graph fills with
`sync-pins` auto-commits (~48/day). Also makes it easy to accidentally grant
admin-token access to a workflow that shouldn't have it.

### Option B — PR-based auto-updates

Change the three workflows to open PRs via `peter-evans/create-pull-request`
instead of direct-pushing. Flux `ImageUpdateAutomation` similarly gets changed
to push to a feature branch and open a PR (non-trivial — Flux's
`ImageUpdatePolicy` pushes directly, no native PR mode; would likely need
changing the push target to a `flux-image-updates` branch and a separate job
to open the PR).

Pros: everything goes through the same gate. Honest audit trail. No shared
admin token in CI.
Cons: a _lot_ of PRs — 48+/day from sync-pins alone, plus flake-update,
plus every image build. Each one triggers CI. Each one needs auto-merge
configured. Auto-merge on required-status-checks rulesets is a whole
separate setup (and has historically been finicky).

### Option C — partial protection

Only gate `main` (future default) and leave `devel` unprotected. Buys nothing
today but sets up the ruleset so a future rename protects the then-default.

### Option D — skip bypass, accept broken workflows

Turn the ruleset on with no bypass, let the three workflows break, fix them
one at a time in follow-up PRs. Aggressive but honest — forces the decision
per-workflow.

## Recommendation

Probably Option A as a pragmatic first step, with a clear "rotate this PAT"
runbook and a tight audit on which workflows reference the new GHA secret. The
contribution-graph noise is cosmetic, and the security delta over today is
small — the PAT already exists in-cluster and is already used for pushes, we're
just granting CI runners access to it.

Decision deferred — see tracking issue.

## Tracking

- GitHub issue: TBD (to be opened as part of this revert PR)
- Related files to restore when the second attempt lands:
  - `cluster/terraform/gitops/github-repo-rulesets/` (module)
  - `cluster/k8s/github-repo-rulesets/` (Flux wiring)
  - `cluster/k8s/kustomization.yaml` (root resource list)
- The PAT must have `Administration: Read and write` fine-grained scope before
  apply — this is already set as of 2026-04-15.
