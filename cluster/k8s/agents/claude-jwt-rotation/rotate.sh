#!/usr/bin/bash
set -euo pipefail

# Materialize a cluster-usable bearer JWT for Claude Code web/CLI sessions.
#
# 1. Authenticate to Authentik's `kubectl-sandbox-client-credentials` OAuth2
#    provider via client_credentials grant (confidential client, no user
#    consent), getting a JWT with hardcoded `groups: ["kubectl-sandbox-users"]`
#    via the kubectl_sandbox_fixed_groups scope mapping.
# 2. Commit the JWT SOPS-encrypted to secrets/claude-web-k8s-jwt.yaml on
#    devel.  write_kubeconfig.py decrypts it at SessionStart and embeds it
#    in the kubeconfig as user.token.
#
# Credentials (client_id / client_secret) come from the k8s Secret
# `kubectl-sandbox-client-credentials` in agents-infra, created by
# tf/gitops/agent-machine-access. They never leave the cluster.

# rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs/ is
# empty. Build a CA bundle from the raw cert files for git/libcurl.
CA_BUNDLE="/tmp/ca-bundle.crt"
cat /usr/share/ca-certificates/mozilla/*.crt >"$CA_BUNDLE"
export GIT_SSL_CAINFO="$CA_BUNDLE"
export CURL_CA_BUNDLE="$CA_BUNDLE"

CLIENT_ID=$(cat /var/run/secrets/authentik-client/client_id)
CLIENT_SECRET=$(cat /var/run/secrets/authentik-client/client_secret)
GITHUB_PAT=$(cat /var/run/secrets/github-pat/token)

TOKEN_URL="https://auth.allegedly.works/application/o/kubectl-sandbox-client-credentials/token/"

JWT=$(curl -sSf -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -d 'grant_type=client_credentials&scope=openid groups' \
  "${TOKEN_URL}" | jq -r .access_token)

if [ -z "$JWT" ] || [ "$JWT" = "null" ]; then
  echo "ERROR: client_credentials exchange returned no access_token" >&2
  exit 1
fi

# Sanity-check: decode the payload and confirm groups claim carries the
# sandbox group — if the scope mapping is missing, this catches it early
# instead of shipping a useless token.
PAYLOAD=$(printf '%s' "$JWT" | cut -d. -f2 | tr '_-' '/+' | base64 -d 2>/dev/null || true)
if ! printf '%s' "$PAYLOAD" | jq -e '.groups | index("kubectl-sandbox-users")' >/dev/null; then
  echo "ERROR: issued JWT does not carry groups: [\"kubectl-sandbox-users\"] — check the fixed_groups scope mapping" >&2
  echo "payload: $PAYLOAD" >&2
  exit 1
fi

# Clone repo and write the SOPS-encrypted token.
git clone --depth=1 --branch=devel \
  "https://x-access-token:${GITHUB_PAT}@github.com/agentydragon/ducktape.git" \
  /tmp/repo
cd /tmp/repo

cat >secrets/claude-web-k8s-jwt.yaml <<EOF
jwt: ${JWT}
EOF

sops encrypt --in-place secrets/claude-web-k8s-jwt.yaml

git config user.name "claude-jwt-rotation"
git config user.email "noreply@allegedly.works"
git add secrets/claude-web-k8s-jwt.yaml

if git diff --cached --quiet; then
  echo "No changes to commit"
else
  git commit -m "chore: rotate Claude web K8s JWT ($(date -I))"
  git push origin devel
fi
