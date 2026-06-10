#!/usr/bin/env bash
# Print a session-context banner about the claude Forgejo service account
# (provisioned by tf/gitops/forgejo-claude). Does NOT export the credentials —
# the agent fetches them on demand via kubectl. Invoked as an after_env
# background command from both CLI and web profiles so the banner content
# lives in one place.
#
# Gate: only prints when SOPS env is available (proxied via
# $BUILDBUDDY_API_KEY, which is set by *_env.sh on successful SOPS decrypt).
# If SOPS isn't set up, kubectl against the cluster also won't work for the
# agent, so the banner would be useless noise.
set -uo pipefail

[ -n "${BUILDBUDDY_API_KEY:-}" ] || exit 0

cat <<'EOF'
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
