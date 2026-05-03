# `ducktape-automation` GitHub App

GitHub App on the `agentydragon` personal account, used as a ruleset
bypass actor for branch-protected default branches on `agentydragon/ducktape`
and `agentydragon/gaffer-private`. Workflows that need to push directly
mint installation tokens via `actions/create-github-app-token` instead of
using `GITHUB_TOKEN`.

## Public identifiers (not secrets)

- App ID: `3590331`
- Client ID: `Iv23liHASG1waUMP1x2o`
- App settings: <https://github.com/settings/apps/ducktape-automation>

The App ID is the `actor_id` for `Integration` bypass actors in
`github_repository_ruleset` resources.

## Secret material (this directory)

- `ducktape-automation.2026-05-03.private-key.sops.pem` — RSA private key,
  used to mint installation tokens. SOPS binary; recipients: admin,
  cluster-secrets, ci.
- `ducktape-automation.2026-05-03.client-secret.sops.txt` — OAuth
  user-to-server client secret. Unused today; encrypted alongside the
  private key for completeness. Recipients: admin only.

Filename date is the issuance date of the current key. To rotate, generate
a new key in the App settings, save as
`ducktape-automation.<new-date>.private-key.sops.pem`, then delete the old
file in a follow-up commit once consumers have switched.

## Permissions (Repository)

- `Contents`: Read and write (push commits to protected branches)
- `Pull requests`: Read and write (open/merge PRs if we ever switch a
  workflow to PR-based auto-updates)
- `Metadata`: Read (forced)

Notably **does not** have `Administration` — the whole point of this App
over the existing `github-secrets-sync-pat` is reduced blast radius.

## Installations

- `agentydragon/ducktape`
- `agentydragon/gaffer-private`
