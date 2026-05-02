<%import os%>\
## Secrets
% if setup.buildbuddy_api_key:
`BUILDBUDDY_API_KEY` loaded (from `devinfra/secrets/cli_env.sh`).
% else:
`BUILDBUDDY_API_KEY` not loaded — Bazel RBE unavailable. Check `devinfra/secrets/cli_env.sh`.
% endif
% if setup.github_token:
`GITHUB_TOKEN` available (personal PAT from home-manager). `gh` CLI and authenticated git operations work.
% endif
`KUBECONFIG` comes from personal config (home-manager `~/.kube/config`), not from the hook daemon. It may or may not be present depending on the user's setup.

% if setup.buildbuddy_api_key:
${"##"} Authentik API
Read-only diagnostics service account token in k8s `claude-sandbox/claude-authentik-api-token` (Reflector-mirrored from `authentik` ns). Service: <https://auth.allegedly.works>. Fetch on demand — not exported to the env:
```
TOKEN=$(kubectl get secret -n claude-sandbox claude-authentik-api-token -o jsonpath='{.data.token}' | base64 -d)
curl -sH "Authorization: Bearer $TOKEN" https://auth.allegedly.works/api/v3/<endpoint>/
```
Permissions: audit log, apps, proxy providers, users, groups, sessions, outposts, flows, policies, system tasks, blueprints. Not available: OAuth2 client secrets, access/refresh tokens, certificate keys. Key endpoints: `/api/v3/events/events/` (audit log), `/api/v3/core/applications/`, `/api/v3/core/users/`, `/api/v3/outposts/instances/`, `/api/v3/admin/system_tasks/`, `/api/v3/flows/instances/`.
## TODO: also commit the token as a SOPS file (e.g. `secrets/claude-authentik-api-token.yaml`)
## with the `codex-cloud-agent` age recipient (and `claude-web` + admin/user keys),
## so codex sessions — which have SOPS but no in-cluster kubectl — can `sops -d` it directly.
% endif

## direnv

The session env file runs `direnv export bash` before every Bash tool call. Environment
variables from `.envrc` files are automatically available. When you `cd` to a different
directory, the next command picks up that directory's `.envrc` because Claude Code sources
the session env file after changing to the tracked working directory.
