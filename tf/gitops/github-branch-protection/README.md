# GitHub Branch Protection

This Terraform module manages branch protection for
`agentydragon/ducktape` through a `github_repository_ruleset` on
`refs/heads/devel` and `refs/heads/main`.

## Current Rules

- Enforcement is `active`.
- Required checks are exact GitHub check-run names:
  - `bazel-ci / Test & Build`
  - `Pre-commit checks`
- Deletion, force-push, and non-linear history are blocked.
- Pull requests are required so required checks can run, but the solo-repo
  review count is zero.

The module runs from Flux at `cluster/k8s/github-branch-protection/` and
authenticates with `flux-system/github-secrets-sync-pat`. That PAT needs
GitHub Administration read/write because GitHub exposes rulesets under the
repository-administration API.

## Bypass Actors

- `RepositoryRole=admin` (`actor_id = 5`): owner pushes and in-cluster
  automations that still push as the owner via PAT.
- `Integration=ducktape-automation` (`actor_id = 3590331`): workflows and Flux
  GitRepository writers that mint installation tokens for the
  `ducktape-automation` GitHub App.

Use the App bypass for automation that must push directly to protected
branches. The built-in `github-actions` integration is not available as a
bypass actor on personal-account repositories, which is why the dedicated App
exists. App identifiers, permissions, and key rotation live in
`secrets/ducktape_automation.README.md`.

## GitHub Plan Limits

`enforcement = "evaluate"` is GitHub Enterprise-only for this account. Applying
that dry-run mode returned:

```text
422 "Enforcement evaluate option is not supported on this plan. Please upgrade to Enterprise"
```

The ruleset therefore went straight to `active` after verifying the exact
required-check contexts against a real PR head and reusing bypass actors from
the previously active main-only ruleset.

`agentydragon/gaffer-private` is not protected by this module. On GitHub Free,
branch protection for private repositories is unavailable for both modern
rulesets and classic branch protection; both APIs returned:

```text
403 Upgrade to GitHub Pro or make this repository public to enable this feature.
```

Protecting `gaffer-private/main` requires upgrading the account to GitHub Pro
or making the repository public. Until then, it relies on repository write
access control plus the `ducktape-automation` App for GitOps writes.

## Remaining Cleanup

- Verify whether GitHub Secret Protection covers push protection on private
  personal-account repos now, then enable or explicitly reject it for
  `gaffer-private`.
- Retire legacy SSH deploy keys superseded by `ducktape-automation` GitHub App
  auth. Cleanup markers are in:
  - `cluster/k8s/gaffer-private-source/deploy-key-tf.yaml`
  - `cluster/k8s/gaffer-private-source/kustomization.yaml`
  - `tf/gitops/gaffer-private-flux/main.tf`
  - the Flux bootstrap git secret/deploy key for `agentydragon/ducktape`
