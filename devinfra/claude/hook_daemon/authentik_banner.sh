#!/usr/bin/env bash
# Print a session-context banner about the read-only Authentik diagnostics
# API token. Does NOT export the token — the agent fetches it on demand
# via kubectl. Invoked as an after_env background command from both CLI and
# web profiles so the banner content lives in one place.
#
# Gate: only prints when SOPS env is available (proxied via
# $BUILDBUDDY_API_KEY, which is set by *_env.sh on successful SOPS decrypt).
# If SOPS isn't set up, kubectl against the cluster also won't work for the
# agent, so the banner would be useless noise.
#
# TODO: also commit the token as a SOPS file (e.g.
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
Permissions: audit log, apps, proxy providers, users, groups, sessions, outposts, flows, policies, system tasks, blueprints. Not available: OAuth2 client secrets, access/refresh tokens, certificate keys. Key endpoints: `/api/v3/events/events/` (audit log), `/api/v3/core/applications/`, `/api/v3/core/users/`, `/api/v3/outposts/instances/`, `/api/v3/admin/system_tasks/`, `/api/v3/flows/instances/`.
EOF
