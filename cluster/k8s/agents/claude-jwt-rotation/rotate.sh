#!/usr/bin/bash
set -euo pipefail

# Materialize a cluster-usable bearer JWT for Claude Code web/CLI sessions.
#
# Runs hourly. Most invocations are no-ops: we sparse-clone just the
# existing $SOPS_FILE + $SOPS_CONFIG, sops-decrypt the JWT, decode its
# payload, and skip rotation if the JWT is currently valid (now ≥ nbf)
# AND has more than $ROTATE_BELOW_HOURS of validity remaining (now < exp − threshold).
# So an Authentik token is actually minted only every ~44 days
# (validity 45d − 1d threshold), but a failed rotation self-heals in <1h.
#
# Mint flow when a rotation IS needed:
# 1. Authenticate to Authentik's `kubectl-sandbox-client-credentials`
#    OAuth2 provider via client_credentials grant (confidential client,
#    no user consent), getting a JWT with hardcoded
#    `groups: ["kubectl-sandbox-users"]` via the
#    kubectl_sandbox_fixed_groups scope mapping.
# 2. Commit the JWT SOPS-encrypted to secrets/claude-web-k8s-jwt.yaml on
#    devel.  write_kubeconfig.py decrypts it at SessionStart and embeds
#    it in the kubeconfig as user.token.
#
# Credentials:
#  - Authentik client_id / client_secret: k8s Secret
#    `kubectl-sandbox-client-credentials` in agents-infra (created by
#    tf/gitops/agent-machine-access).
#  - SOPS age key for decrypting the existing JWT file: k8s Secret
#    `sops-age-cluster-secrets`, mirrored from flux-system to agents-infra
#    via reflector annotations on the TF-managed source secret (see
#    cluster/terraform/main/persistent-auth.tf).
#  - GitHub PAT for the commit/push: existing github-secrets-sync-pat.

ROTATE_BELOW_HOURS=24
SOPS_FILE="secrets/claude-web-k8s-jwt.yaml"
SOPS_CONFIG=".sops.yaml"
GITHUB_REPO="agentydragon/ducktape"
TOKEN_URL="https://auth.allegedly.works/application/o/kubectl-sandbox-client-credentials/token/"

# rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs/ is
# empty. Build a CA bundle from the raw cert files for git/libcurl.
CA_BUNDLE="/tmp/ca-bundle.crt"
cat /usr/share/ca-certificates/mozilla/*.crt >"$CA_BUNDLE"
export GIT_SSL_CAINFO="$CA_BUNDLE"
export CURL_CA_BUNDLE="$CA_BUNDLE"

CLIENT_ID=$(cat /var/run/secrets/authentik-client/client_id)
CLIENT_SECRET=$(cat /var/run/secrets/authentik-client/client_secret)
GITHUB_PAT=$(cat /var/run/secrets/github-pat/token)
export SOPS_AGE_KEY_FILE=/var/run/secrets/sops-age/age.agekey

# Decode a JWT's base64url-encoded payload to JSON on stdout.
# (base64 needs '=' padding and '+' '/' alphabet; JWT uses '-' '_' and no padding.)
decode_jwt_payload() {
  local jwt="$1"
  local p
  p=$(printf '%s' "$jwt" | cut -d. -f2 | tr '_-' '/+')
  case $((${#p} % 4)) in
    2) p="${p}==" ;;
    3) p="${p}=" ;;
  esac
  printf '%s' "$p" | base64 -d
}

# --- Sparse clone (just $SOPS_FILE + $SOPS_CONFIG, ~few KB) ---------------
mkdir /tmp/repo
cd /tmp/repo
git init -q
git remote add origin "https://x-access-token:${GITHUB_PAT}@github.com/${GITHUB_REPO}.git"
git config core.sparseCheckout true
{
  echo "$SOPS_FILE"
  echo "$SOPS_CONFIG"
} >.git/info/sparse-checkout
git fetch -q --depth=1 --no-tags origin devel
git checkout -q FETCH_HEAD

# --- Freshness check on existing JWT --------------------------------------
if [ -f "$SOPS_FILE" ]; then
  EXISTING_JWT=$(sops -d --extract '["jwt"]' "$SOPS_FILE")
  PAYLOAD=$(decode_jwt_payload "$EXISTING_JWT")
  EXP=$(printf '%s' "$PAYLOAD" | jq -r '.exp')
  NBF=$(printf '%s' "$PAYLOAD" | jq -r '.nbf // .iat // 0')
  NOW=$(date +%s)
  REMAINING_H=$(((EXP - NOW) / 3600))
  if [ "$NOW" -lt "$NBF" ]; then
    echo "Existing JWT not yet valid (nbf=$NBF, now=$NOW); rotating"
  elif [ "$REMAINING_H" -gt "$ROTATE_BELOW_HOURS" ]; then
    echo "Existing JWT valid for ${REMAINING_H}h > ${ROTATE_BELOW_HOURS}h threshold; skipping rotation"
    exit 0
  else
    echo "Existing JWT expires in ${REMAINING_H}h; rotating"
  fi
else
  echo "No existing $SOPS_FILE on devel; bootstrapping initial rotation"
fi

# --- Mint a fresh JWT via client_credentials -------------------------------
JWT=$(curl -sSf -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -d 'grant_type=client_credentials&scope=openid groups' \
  "${TOKEN_URL}" | jq -r .access_token)

if [ -z "$JWT" ] || [ "$JWT" = "null" ]; then
  echo "ERROR: client_credentials exchange returned no access_token" >&2
  exit 1
fi

# Sanity-check: confirm groups claim carries the sandbox group.
PAYLOAD=$(decode_jwt_payload "$JWT")
if ! printf '%s' "$PAYLOAD" | jq -e '.groups | index("kubectl-sandbox-users")' >/dev/null; then
  echo "ERROR: issued JWT does not carry groups: [\"kubectl-sandbox-users\"] — check the fixed_groups scope mapping" >&2
  echo "payload: $PAYLOAD" >&2
  exit 1
fi

# --- Write + commit + push -------------------------------------------------
cat >"$SOPS_FILE" <<EOF
jwt: ${JWT}
EOF

sops encrypt --in-place "$SOPS_FILE"

git config user.name "claude-jwt-rotation"
git config user.email "noreply@allegedly.works"
git add "$SOPS_FILE"

if git diff --cached --quiet; then
  echo "No changes to commit"
else
  git commit -q -m "chore: rotate Claude web K8s JWT ($(date -I))"
  git push -q origin HEAD:devel
fi
