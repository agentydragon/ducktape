#!/usr/bin/bash
set -euo pipefail

# Materialize an Authentik-issued bearer JWT into a SOPS-encrypted file.
#
# Runs hourly. Most invocations are no-ops: we sparse-clone just the existing
# $SOPS_FILE + $SOPS_CONFIG, read the unencrypted-by-suffix
# `expires_unencrypted` field with sed (no SOPS decryption, no in-cluster
# age-key access), and skip rotation when remaining validity exceeds
# $ROTATE_BELOW_HOURS. So an Authentik token is actually minted only every
# ~44 days (validity 45d − 1d threshold), but a failed rotation self-heals in
# <1h.
#
# `expires_unencrypted` is set from the JWT's own `exp` claim at write-time
# below — single source of truth, no constant duplicated from Authentik
# provider config. SOPS leaves the field plaintext because it ends in the
# default unencrypted_suffix `_unencrypted`.
#
# Parameterization:
#   ROTATION_NAME             Human-readable name for logs / commits
#   AUTHENTIK_PROVIDER_SLUG   Provider slug used to verify the source JWT issuer
#   TOKEN_SCOPES              OAuth scopes for the source client_credentials mint
#   EXCHANGE_TOKEN_SCOPES     Optional scopes for a second-stage JWT-bearer exchange.
#                             When set, the script first mints a source JWT from
#                             AUTHENTIK_PROVIDER_SLUG, then immediately exchanges it
#                             into a token whose provider is the proxy provider named
#                             by $EXCHANGE_CLIENT_ID_FILE. This is the working
#                             Authentik pattern for proxy outposts: direct Bearer
#                             auth only succeeds when the final token was issued by
#                             the proxy provider itself.
#   EXCHANGE_CLIENT_ID_FILE   File containing the proxy provider's client_id for the
#                             second-stage exchange. Defaults to
#                             $AUTHENTIK_CLIENT_DIR/proxy_client_id.
#   SOPS_FILE                 Repo path to the encrypted output file
#   TOKEN_FIELD_NAME          YAML field name for the encrypted token (jwt/token)
#   EXPECTED_GROUP            Optional source-JWT group claim that must be present
#   ROTATE_BELOW_HOURS        Freshness threshold before a new token is minted

: "${ROTATION_NAME:?ROTATION_NAME is required}"
: "${AUTHENTIK_PROVIDER_SLUG:?AUTHENTIK_PROVIDER_SLUG is required}"
: "${SOPS_FILE:?SOPS_FILE is required}"

ROTATE_BELOW_HOURS="${ROTATE_BELOW_HOURS:-24}"
TOKEN_SCOPES="${TOKEN_SCOPES:-openid profile email}"
EXCHANGE_TOKEN_SCOPES="${EXCHANGE_TOKEN_SCOPES:-}"
TOKEN_FIELD_NAME="${TOKEN_FIELD_NAME:-jwt}"
EXPECTED_GROUP="${EXPECTED_GROUP:-}"
SOPS_CONFIG="${SOPS_CONFIG:-.sops.yaml}"
GITHUB_REPO="${GITHUB_REPO:-agentydragon/ducktape}"
TOKEN_URL="${TOKEN_URL:-https://auth.allegedly.works/application/o/token/}"
AUTHENTIK_CLIENT_DIR="${AUTHENTIK_CLIENT_DIR:-/var/run/secrets/authentik-client}"
EXCHANGE_CLIENT_ID_FILE="${EXCHANGE_CLIENT_ID_FILE:-${AUTHENTIK_CLIENT_DIR}/proxy_client_id}"
GITHUB_PAT_FILE="${GITHUB_PAT_FILE:-/var/run/secrets/github-pat/token}"
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-authentik-jwt-rotation}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-noreply@allegedly.works}"
COMMIT_MESSAGE_PREFIX="${COMMIT_MESSAGE_PREFIX:-chore: rotate ${ROTATION_NAME}}"
SOURCE_EXPECTED_ISSUER="https://auth.allegedly.works/application/o/${AUTHENTIK_PROVIDER_SLUG}/"

# rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs/ is
# empty. Build a CA bundle from the raw cert files for git/libcurl.
CA_BUNDLE="/tmp/ca-bundle.crt"
cat /usr/share/ca-certificates/mozilla/*.crt >"$CA_BUNDLE"
export GIT_SSL_CAINFO="$CA_BUNDLE"
export CURL_CA_BUNDLE="$CA_BUNDLE"

CLIENT_ID=$(cat "${AUTHENTIK_CLIENT_DIR}/client_id")
CLIENT_SECRET=$(cat "${AUTHENTIK_CLIENT_DIR}/client_secret")
GITHUB_PAT=$(cat "$GITHUB_PAT_FILE")

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

mint_source_jwt() {
  curl -sSf -u "${CLIENT_ID}:${CLIENT_SECRET}" \
    -d grant_type=client_credentials \
    --data-urlencode "scope=${TOKEN_SCOPES}" \
    "${TOKEN_URL}" | jq -r '.access_token // empty'
}

exchange_jwt_for_proxy_provider() {
  local source_jwt="$1"
  local exchange_client_id="$2"
  curl -sSf \
    -d grant_type=client_credentials \
    --data-urlencode "client_id=${exchange_client_id}" \
    --data-urlencode "client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer" \
    --data-urlencode "client_assertion=${source_jwt}" \
    --data-urlencode "scope=${EXCHANGE_TOKEN_SCOPES}" \
    "${TOKEN_URL}" | jq -r '.access_token // empty'
}

# --- Sparse clone (just $SOPS_FILE + $SOPS_CONFIG, ~few KB) ---------------
# .sops.yaml is needed by `sops encrypt --in-place` later to find the right
# recipient set. The freshness check itself only needs $SOPS_FILE.
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

# --- Freshness check on existing JWT (no decryption needed) ---------------
if [ -f "$SOPS_FILE" ]; then
  EXISTING_EXPIRES=$(sed -n 's/^expires_unencrypted:[[:space:]]*//p' "$SOPS_FILE" | head -n1 | tr -d '"')
  if [ -n "$EXISTING_EXPIRES" ]; then
    EXPIRES_TS=$(date -u -d "$EXISTING_EXPIRES" +%s)
    NOW_TS=$(date +%s)
    REMAINING_H=$(((EXPIRES_TS - NOW_TS) / 3600))
    if [ "$REMAINING_H" -gt "$ROTATE_BELOW_HOURS" ]; then
      echo "${ROTATION_NAME}: existing token expires at $EXISTING_EXPIRES (${REMAINING_H}h remaining > ${ROTATE_BELOW_HOURS}h threshold); skipping rotation"
      exit 0
    fi
    echo "${ROTATION_NAME}: existing token expires at $EXISTING_EXPIRES (${REMAINING_H}h remaining); rotating"
  else
    echo "${ROTATION_NAME}: existing $SOPS_FILE has no expires_unencrypted field; rotating to populate it"
  fi
else
  echo "${ROTATION_NAME}: no existing $SOPS_FILE on devel; bootstrapping initial rotation"
fi

# --- Mint a fresh source JWT via client_credentials ------------------------
SOURCE_JWT=$(mint_source_jwt)

if [ -z "$SOURCE_JWT" ]; then
  echo "ERROR: ${ROTATION_NAME}: client_credentials exchange returned no access_token" >&2
  exit 1
fi

# Decode the source payload first. For exchange-based rotations this is the
# assertion token Authentik validates against jwt_federation_providers; for
# direct rotations it is also the final token that gets written to SOPS.
SOURCE_PAYLOAD=$(decode_jwt_payload "$SOURCE_JWT")
if ! printf '%s' "$SOURCE_PAYLOAD" | jq -e --arg issuer "$SOURCE_EXPECTED_ISSUER" '.iss == $issuer' >/dev/null; then
  echo "ERROR: ${ROTATION_NAME}: issued source JWT has unexpected issuer (wanted ${SOURCE_EXPECTED_ISSUER})" >&2
  echo "payload: $SOURCE_PAYLOAD" >&2
  exit 1
fi
if [ -n "$EXPECTED_GROUP" ] && ! printf '%s' "$SOURCE_PAYLOAD" | jq -e --arg group "$EXPECTED_GROUP" '(.groups // []) | index($group)' >/dev/null; then
  echo "ERROR: ${ROTATION_NAME}: issued source JWT does not carry groups: [\"${EXPECTED_GROUP}\"]" >&2
  echo "payload: $SOURCE_PAYLOAD" >&2
  exit 1
fi

# Some consumers need the raw OAuth2 provider JWT directly; others need a
# proxy-provider-scoped JWT so the Authentik outpost's provider-scoped
# introspection will accept it. The latter is the same two-hop pattern used by
# the working MCP backends in this repo.
JWT="$SOURCE_JWT"
PAYLOAD="$SOURCE_PAYLOAD"
if [ -n "$EXCHANGE_TOKEN_SCOPES" ]; then
  if [ ! -f "$EXCHANGE_CLIENT_ID_FILE" ]; then
    echo "ERROR: ${ROTATION_NAME}: EXCHANGE_TOKEN_SCOPES is set but ${EXCHANGE_CLIENT_ID_FILE} does not exist" >&2
    exit 1
  fi
  EXCHANGE_CLIENT_ID=$(cat "$EXCHANGE_CLIENT_ID_FILE")
  if [ -z "$EXCHANGE_CLIENT_ID" ]; then
    echo "ERROR: ${ROTATION_NAME}: ${EXCHANGE_CLIENT_ID_FILE} is empty" >&2
    exit 1
  fi

  JWT=$(exchange_jwt_for_proxy_provider "$SOURCE_JWT" "$EXCHANGE_CLIENT_ID")
  if [ -z "$JWT" ]; then
    echo "ERROR: ${ROTATION_NAME}: proxy-provider token exchange returned no access_token" >&2
    exit 1
  fi
  if [ "$JWT" = "$SOURCE_JWT" ]; then
    echo "ERROR: ${ROTATION_NAME}: proxy-provider token exchange returned the source JWT unchanged" >&2
    exit 1
  fi
  PAYLOAD=$(decode_jwt_payload "$JWT")
fi

# Capture the expiry from the actual token we write to SOPS. For exchange-based
# rotations the proxy provider's token lifetime can differ from the source
# provider's token lifetime, so the freshness check must key off the final token.
EXP_TS=$(printf '%s' "$PAYLOAD" | jq -r '.exp')
EXPIRES_ISO=$(date -u -d "@${EXP_TS}" +%Y-%m-%dT%H:%M:%SZ)

# --- Write + commit + push -------------------------------------------------
# `expires_unencrypted` matches SOPS's default unencrypted_suffix (`_unencrypted`),
# so it stays plaintext after `sops encrypt --in-place`.
mkdir -p "$(dirname "$SOPS_FILE")"
cat >"$SOPS_FILE" <<EOF
expires_unencrypted: "$EXPIRES_ISO"
${TOKEN_FIELD_NAME}: $JWT
EOF

sops encrypt --in-place "$SOPS_FILE"

git config user.name "$GIT_AUTHOR_NAME"
git config user.email "$GIT_AUTHOR_EMAIL"
git add "$SOPS_FILE"

if git diff --cached --quiet; then
  echo "${ROTATION_NAME}: no changes to commit"
else
  git commit -q -m "${COMMIT_MESSAGE_PREFIX} ($(date -I))"
  git push -q origin HEAD:devel
fi
