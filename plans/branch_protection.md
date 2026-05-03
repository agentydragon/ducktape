# Branch protection for `agentydragon/ducktape` and `agentydragon/gaffer-private`

## Goal

Enforce that commits landing on the default branch of either repo have passed
CI checks. Concretely, gate on:

- ducktape: `bazel-ci / Test & Build` and `Pre-commit checks`
- gaffer-private: `Bazel CI / Test & Build` (and `Pre-commit checks` once
  gaffer's pre-commit workflow is created — separate PR)

Plus the cheap extras: block branch deletion, force-push, non-linear merge
commits.

## Current state (as of this commit)

- `tf/gitops/github-branch-protection/` — module landed. Two
  `github_repository_ruleset` resources, both targeting `refs/heads/main`,
  authenticated via the existing per-repo PATs (`github-secrets-sync-pat` for
  ducktape, `github-pat-gaffer-private-flux` for gaffer).
- ducktape main: `enforcement = "active"`. No-op today because main is not the
  default branch (`devel` still is) and nothing pushes to main. Will start
  gating direct pushes once the default branch flips to main.
- gaffer-private main: `enforcement = "disabled"`. Active enforcement is
  blocked on Flux's `gaffer-images` ImageUpdateAutomation, which pushes to
  main via SSH using `gaffer-private-deploy-key` — deploy keys are not a
  user/App identity, so neither the `RepositoryRole=admin` nor the
  `Integration=ducktape-automation` bypass actor matches them. See "Pending:
  Flux App migration" below.
- Bypass actors on both rulesets:
  - `RepositoryRole=admin` (id 5) — covers in-cluster automations pushing as
    the owner via PAT (`claude-token-rotation`, `attic-jwt-rotation` CronJobs).
  - `Integration=ducktape-automation` (App ID 3590331) — covers any GHA
    workflow that mints an installation token via
    `actions/create-github-app-token`. Currently no workflow uses the App;
    the three direct-push workflows on ducktape (`sync-pins.yml`,
    `nix-flake-update.yml`, `container-images.yml`'s `pin-digests` job) carry
    TODO markers about migrating when ducktape's default flips.

The `ducktape-automation` GitHub App is registered on the
`agentydragon` personal account; public identifiers and permissions are
documented at <secrets/ducktape-automation.README.md>. The private key is
SOPS-encrypted at <secrets/ducktape-automation.2026-05-03.private-key.sops.pem>.

## Pending: Flux App migration (separate PR)

To activate the gaffer-private ruleset and prepare ducktape for the eventual
default-branch flip, Flux's git push auth needs to move from SSH deploy keys
to ducktape-automation App auth. Flux source-controller and
image-automation-controller (≥ v2.5) accept Secrets containing
`githubAppID`, `githubAppInstallationID`, `githubAppPrivateKey` (and
optionally `githubAppBaseURL` for GHE).

### Migration runbook

1. **Verify App installation.** `ducktape-automation` must be installed on
   both `agentydragon/ducktape` and `agentydragon/gaffer-private`. Check at
   <https://github.com/settings/installations>; install if missing. Note
   each installation ID — visible in the URL of the install's page
   (`https://github.com/settings/installations/<id>`).
2. **Author SOPS-encrypted Secrets** (one per repo) at
   `cluster/k8s/github-app-automation/secrets/{ducktape,gaffer}-flux-auth.sops.yaml`,
   each containing `githubAppID`, `githubAppInstallationID`,
   `githubAppPrivateKey`. Encrypted to `admin + cluster-secrets` recipients
   so Flux can decrypt. The private key is the plaintext PEM extracted from
   `secrets/ducktape-automation.2026-05-03.private-key.sops.pem`.
3. **Wire the Secrets into Flux** via a new `cluster/k8s/github-app-automation/`
   Kustomization (mirrors the shape of `cluster/k8s/github-secrets-sync/secrets/`).
4. **Swap `secretRef`** on:
   - `cluster/k8s/flux-system/gotk-sync.yaml` (the `flux-system` GitRepository) —
     URL also flips from `ssh://...` to `https://github.com/...`.
   - `cluster/k8s/gaffer-private-source/source.yaml` (the `gaffer-private`
     GitRepository) — same URL transition.
5. **Verify Flux pushes succeed.** Force a reconcile of `gaffer-images`
   ImageUpdateAutomation; confirm a commit lands on gaffer's `main` attributed
   to the App. Force a reconcile of ducktape's `all-images` similarly.
6. **Activate the gaffer ruleset.** Flip
   `github_repository_ruleset.gaffer_main.enforcement` from `"disabled"` to
   `"active"` in `tf/gitops/github-branch-protection/main.tf`.
7. **Retire the deploy keys** in a follow-up tombstone commit:
   `tf/gitops/gaffer-private-flux/` and the equivalent ducktape flux-system
   deploy-key setup. Both currently have `prevent_destroy` lifecycle blocks
   that need removal first.

The migration is intentionally split from this PR because step 5 is a
"watch the logs" cutover — clean failure modes are important and rolling
back a multi-file change is more annoying than rolling back the
ruleset-only change.

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
