# Branch protection for `agentydragon/ducktape` and `agentydragon/gaffer-private`

## Goal

Enforce that commits landing on the default branch of either repo have passed
CI checks. Concretely, gate on:

- ducktape: `bazel-ci / Test & Build` and `Pre-commit checks`
- gaffer-private: `Test & Build` and `Pre-commit checks` (PR-merge gate only —
  see "gaffer-private trade-off" below)

Plus the cheap extras: block branch deletion, force-push, non-linear merge
commits.

## Current state

- `tf/gitops/github-branch-protection/` — module landed. Two protection
  resources, one per repo, authenticated via the existing per-repo PATs
  (`github-secrets-sync-pat` for ducktape, `github-pat-gaffer-private-flux`
  for gaffer).
- ducktape main: `github_repository_ruleset` with `enforcement = "active"`.
  No-op today because main is not the default branch (`devel` still is) and
  nothing pushes to main. Will start gating direct pushes once the default
  branch flips to main. Bypass actors:
  - `RepositoryRole=admin` (id 5) — covers in-cluster automations pushing as
    the owner via PAT (`claude-token-rotation`, `attic-jwt-rotation` CronJobs).
  - `Integration=ducktape-automation` (App ID 3590331) — covers GHA workflows
    that mint an installation token via `actions/create-github-app-token`,
    **and** Flux's source-controller / image-automation-controller pushes on
    the `flux-system` GitRepository (after commit 745532e6b). The three
    direct-push workflows on ducktape (`sync-pins.yml`, `nix-flake-update.yml`,
    `container-images.yml`'s `pin-digests` job) still carry TODO markers
    about migrating when ducktape's default flips.
- gaffer-private main: classic `github_branch_protection`. Rulesets are not
  available on Free private repos (POST returns 403 "Upgrade to GitHub Pro
  or make this repository public"). PR merges via the merge button are gated
  on `Test & Build` and `Pre-commit checks` passing; deletion / force-push /
  non-linear merges are blocked. Direct pushes from `Contents:write` actors
  (you, the App, PAT-using CronJobs) bypass the CI gate — this is the
  trade-off; see below.

## gaffer-private trade-off

Classic branch protection on Free private repos has a hard gap that
rulesets don't have: **`required_status_checks` only enforces on PR merges,
not on direct `git push`.** The two GitHub features that _would_ close the
gap are both unavailable on the current plan:

- **Rulesets** (modern API, supports `bypass_actors{Integration=...}` to
  exempt the App while still gating everything else): blocked by GitHub
  Pro requirement on private repos.
- **`restrict_pushes` on classic protection** (whitelist of users/apps
  allowed to push at all): blocked by Pro requirement.

The remaining lever — `required_pull_request_reviews` — would force ALL
writes through PRs, which would block Flux's `gaffer-images`
ImageUpdateAutomation since it pushes commits directly without opening
PRs. Net: no way on Free to gate direct pushes on CI without breaking
Flux.

We accept the gap because the set of actors with `Contents:write` on
gaffer-private is small and trusted (you, ducktape-automation App,
github-secrets-sync-pat for in-cluster automations). The threat
"unauthorized actor pushes red commit" is gated by access control; the
threat "trusted actor accidentally pushes red commit directly" is the
residual risk.

To close the gap pick one when you're ready:

1. **GitHub Pro upgrade** (~$4/mo). Switch gaffer to a ruleset mirroring
   ducktape's. Cleanest.
2. **Migrate `gaffer-images` ImageUpdateAutomation to PR mode.** Push to a
   feature branch + auto-open + auto-merge a PR. With direct-push
   automation gone, classic protection's `required_pull_request_reviews`
   becomes safe to enable, fully closing the gap on Free.

Push protection (secret scanning) is independent of branch protection and
is a separate enable-on-the-repo step worth picking up — see "Outstanding
work" below.

The `ducktape-automation` GitHub App is registered on the
`agentydragon` personal account; public identifiers and permissions are
documented at <secrets/ducktape_automation.README.md>. The private key is
SOPS-encrypted at <secrets/ducktape-automation.2026-05-03.private-key.sops.pem>.

## Flux App migration — done in commit 745532e6b

Flux source-controller (v1.7.4) and image-automation-controller (v1.0.4) now
use the `ducktape-automation` GitHub App for git auth on both the
`flux-system` (ducktape) and `gaffer-private` GitRepositories. Both
GitRepositories switched from `ssh://git@github.com/…` to
`https://github.com/…` URLs as part of the change.

**Variance from the original runbook**:

- Original plan: one Secret per repo at
  `cluster/k8s/github-app-automation/secrets/{ducktape,gaffer}-flux-auth.sops.yaml`.
- What landed: a **single shared Secret**
  `flux-system/ducktape-automation-github-app` at
  `cluster/k8s/flux-system/ducktape-automation-github-app.sops.yaml`. Both
  GitRepositories reference it. The App is installed user-level with access
  to both repos, so one `githubAppInstallationID` (`129264096`) covers both.
  Simpler kustomize wiring; one secret to rotate.
- Original plan referenced "≥ v2.5". Flux's umbrella version is meta — the
  per-controller minimums for GitHub App auth are source-controller ≥ v1.4
  and image-automation-controller ≥ v0.39 (we're on v1.7.4 / v1.0.4).

## Outstanding work

1. **Close the gaffer direct-push gate gap** via Pro upgrade or PR-mode
   migration of Flux image automation; see "gaffer-private trade-off" above.

2. **Enable secret-scanning push protection on gaffer-private.** GitHub's
   push protection blocks pushes that contain secrets at the moment of
   `git push`, irrespective of branch protection. Free for public repos
   (already on for ducktape by default since 2024). On private repos it
   historically required GitHub Advanced Security; the personal-account
   "Secret Protection" SKU may now cover it — verify before assuming. Set
   on the repo via the API or web UI; can be terraform-managed via
   `github_repository.security_and_analysis.secret_scanning_push_protection`.

3. **Retire the legacy SSH deploy keys.** CLEANUP markers in place:
   - `cluster/k8s/gaffer-private-source/deploy-key-tf.yaml` header +
     `cluster/k8s/gaffer-private-source/kustomization.yaml` inline — drop
     `deploy-key-tf.yaml` and `github-pat-gaffer-private-flux.sops.yaml`
     from the kustomize resources list.
   - `tf/gitops/gaffer-private-flux/main.tf` header — strip
     `prevent_destroy` lifecycle blocks, `tofu destroy` the module (revokes
     the GitHub deploy key, deletes the in-cluster Secret), then delete the
     module directory + BUILD.bazel target.
   - Ducktape-side `flux-system` SSH Secret + corresponding GitHub deploy
     key on `agentydragon/ducktape`: revoke the deploy key in repo settings;
     the in-cluster Secret is provisioned by the `flux_bootstrap_git` tofu
     resource and needs the bootstrap config updated to stop creating it.

## Workflow migration (also pending, ducktape-side, lower urgency)

When ducktape's default branch flips from `devel` to `main`, the three GHA
workflows that direct-push to the default branch need to migrate from
`GITHUB_TOKEN` to App-minted installation tokens. In-place TODOs already
mark each:

| Workflow                                   | Cadence       | What it pushes                  |
| ------------------------------------------ | ------------- | ------------------------------- |
| `sync-pins.yml`                            | every 30m     | `npins/sources.json` updates    |
| `nix-flake-update.yml`                     | manual        | `flake.lock` updates            |
| `container-images.yml` (`pin-digests` job) | on image push | `cluster/**` image digest bumps |

The migration pattern per workflow:

```yaml
- uses: actions/create-github-app-token@v1
  id: app-token
  with:
    app-id: ${{ vars.AUTOMATION_APP_ID }} # or hardcode 3590331
    private-key: ${{ secrets.AUTOMATION_APP_PRIVATE_KEY }}
- uses: actions/checkout@v6
  with:
    token: ${{ steps.app-token.outputs.token }}
# … push step uses GITHUB_TOKEN → swap to ${{ steps.app-token.outputs.token }}
```

The PEM is in `secrets/` and decryptable in CI (the SOPS file is encrypted
to the `&ci` recipient = the GHA SOPS_AGE_KEY), so the workflow can simply
`sops -d` it inline rather than going through a fresh
`github-secrets-sync` round-trip into GHA Actions Secrets. `sync-pins.yml`
additionally has a hardcoded `devel` ref + push target that has to flip to
`main` at the same time.

## Historical context

PR #1312 (commit `c23edda`, reverted) was the first attempt. It targeted
both `devel` and `main` in one ruleset and tried to use the built-in
`github-actions` (Integration id=15368) as a bypass actor. The latter failed
with a 422 because GitHub doesn't expose `github-actions` as a bypass actor
on personal-account repos — only third-party Apps (e.g. BuildBuddy) appear
in the picker. The fix is registering our own App (`ducktape-automation`)
and using it as the Integration bypass actor; that's the path this work
has taken.

## Tracking

- GitHub issue: agentydragon/ducktape#1314
