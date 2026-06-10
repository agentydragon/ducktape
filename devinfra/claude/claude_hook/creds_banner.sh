#!/usr/bin/env bash
# Print a session-context banner about the in-cluster credentials available to
# the agent on demand: the read-only Authentik diagnostics API token and the
# read-only `claude` Forgejo service account. Does NOT export anything — the
# agent fetches the secrets via kubectl when needed. Invoked as an after_env
# background command from both CLI and web profiles so the banner content
# lives in one place.
#
# Gate: only prints when SOPS env is available (proxied via
# $BUILDBUDDY_API_KEY, which is set by *_env.sh on successful SOPS decrypt).
# If SOPS isn't set up, kubectl against the cluster also won't work for the
# agent, so the banner would be useless noise.
#
# TODO: also commit the Authentik token as a SOPS file (e.g.
# secrets/claude-authentik-api-token.yaml) with the codex-cloud-agent age
# recipient (and claude-web + admin/user keys), so codex sessions — which
# have SOPS but no in-cluster kubectl — can `sops -d` it directly. Then
# this banner can offer both fetch paths.
set -uo pipefail

[ -n "${BUILDBUDDY_API_KEY:-}" ] || exit 0

cat <<'EOF'
## Authentik API
Read-only diagnostics service account token in k8s `claude-sandbox/claude-authentik-api-token` (Reflector-mirrored from `authentik` ns). Service: <https://auth.allegedly.works>. Fetch on demand — not exported to the env:
```
TOKEN=$(kubectl get secret -n claude-sandbox claude-authentik-api-token -o jsonpath='{.data.token}' | base64 -d)
curl -sH "Authorization: Bearer $TOKEN" https://auth.allegedly.works/api/v3/<endpoint>/
```
Permissions: audit log, apps, proxy providers, users, groups, sessions, outposts, flows, policies, system tasks, blueprints. Not available: OAuth2 client secrets, access/refresh tokens, certificate keys. Key endpoints under `/api/v3/`: `events/events/` (audit log), `core/{applications,users}/`, `outposts/instances/`, `admin/system_tasks/`, `flows/instances/`.

## Forgejo (git.allegedly.works)
Read-only Forgejo service account `claude` for agent sessions; HTTP Basic credentials in k8s `claude-sandbox/claude-forgejo-credentials`. Fetch on demand — not exported to the env:
```
U=$(kubectl get secret -n claude-sandbox claude-forgejo-credentials -o jsonpath='{.data.username}' | base64 -d)
P=$(kubectl get secret -n claude-sandbox claude-forgejo-credentials -o jsonpath='{.data.password}' | base64 -d)
git clone "https://$U:$P@git.allegedly.works/<owner>/<repo>.git"
curl -su "$U:$P" https://git.allegedly.works/api/v1/user/repos   # list repos the account can read
```
In-cluster URL: `http://forgejo-http.forgejo:3000`. The account holds read-only collaborations on private data repos, e.g. `thrive-scrape/thrive-scrape` (weekly Thrive Market catalog scrapes: per-page raw API responses + `products.json`; history via `git log`).
EOF
