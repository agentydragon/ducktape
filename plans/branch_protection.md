# Branch protection for `agentydragon/ducktape` and `agentydragon/gaffer-private`

## Goal

Enforce that commits landing on the default branch of either repo have passed
CI checks. Concretely, gate on:

- ducktape: `bazel-ci / Test & Build` and `Pre-commit checks`
- gaffer-private: `Bazel CI / Test & Build` (and `Pre-commit checks` once
  gaffer's pre-commit workflow is created — separate PR)

Plus the cheap extras: block branch deletion, force-push, non-linear merge
commits.

## Current state

- `tf/gitops/github-branch-protection/` — module landed. Two
  `github_repository_ruleset` resources, both targeting `refs/heads/main`,
  authenticated via the existing per-repo PATs (`github-secrets-sync-pat` for
  ducktape, `github-pat-gaffer-private-flux` for gaffer).
- ducktape main: `enforcement = "active"`. No-op today because main is not the
  default branch (`devel` still is) and nothing pushes to main. Will start
  gating direct pushes once the default branch flips to main.
- gaffer-private main: `enforcement = "active"`. Gates `Test & Build` and
  `Pre-commit checks` (the latter from the workflow added in
  `agentydragon/gaffer-private#16`). Flux's `gaffer-images`
  ImageUpdateAutomation pushes via the `ducktape-automation` GitHub App,
  matching the `Integration` bypass actor.
- Bypass actors on both rulesets:
  - `RepositoryRole=admin` (id 5) — covers in-cluster automations pushing as
    the owner via PAT (`claude-token-rotation`, `attic-jwt-rotation` CronJobs).
  - `Integration=ducktape-automation` (App ID 3590331) — covers any GHA
    workflow that mints an installation token via
    `actions/create-github-app-token`, **and as of commit 745532e6b also
    covers Flux's source-controller and image-automation-controller pushes**
    on both the `flux-system` and `gaffer-private` GitRepositories. The three
    direct-push workflows on ducktape (`sync-pins.yml`, `nix-flake-update.yml`,
    `container-images.yml`'s `pin-digests` job) still carry TODO markers
    about migrating when ducktape's default flips.

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

1. **Retire the legacy SSH deploy keys.** CLEANUP markers in place:
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
