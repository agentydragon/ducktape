#!/usr/bin/bash
set -euo pipefail

# Rotate all Attic JWTs listed in /config/rotators.json into their
# SOPS-encrypted destination files.
#
# Per-token freshness check: read the unencrypted-by-suffix
# `expires_unencrypted` field from each existing SOPS file with sed (no
# decryption needed) and skip rotation when remaining validity exceeds
# $ROTATE_BELOW_HOURS. With 1-year validity and a 24h threshold, a real
# mint runs ~once per token per year; failed rotations self-heal in <1h.
#
# `expires_unencrypted` is set from the JWT's own `exp` claim at
# write-time. SOPS leaves the field plaintext because it ends in the
# default unencrypted_suffix `_unencrypted`.
#
# JWT minting goes through `kubectl exec deploy/attic -n nix-cache --
# atticadm -f /config/server.toml make-token …`, so the HS256 signing
# secret never leaves the attic pod. The rotator's ServiceAccount only
# needs `pods/exec` on the attic deployment in the nix-cache namespace.
#
# Single sparse-clone pulls all destination SOPS files at once. Single
# combined commit covers all tokens that actually rotated this cycle.

CONFIG_FILE="${ROTATORS_CONFIG:-/config/rotators.json}"
ATTIC_NAMESPACE="${ATTIC_NAMESPACE:-nix-cache}"
ATTIC_DEPLOYMENT="${ATTIC_DEPLOYMENT:-deploy/attic}"
TOKEN_FIELD_NAME="${TOKEN_FIELD_NAME:-attic_token}"
ROTATE_BELOW_HOURS="${ROTATE_BELOW_HOURS:-24}"
SOPS_CONFIG="${SOPS_CONFIG:-.sops.yaml}"
GITHUB_REPO="${GITHUB_REPO:-agentydragon/ducktape}"
GITHUB_PAT_FILE="${GITHUB_PAT_FILE:-/var/run/secrets/github-pat/token}"
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-attic-jwt-rotation}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-noreply@allegedly.works}"

# rules_distroless doesn't run update-ca-certificates, so /etc/ssl/certs/ is
# empty. Build a CA bundle from the raw cert files for git/libcurl.
CA_BUNDLE="/tmp/ca-bundle.crt"
cat /usr/share/ca-certificates/mozilla/*.crt >"$CA_BUNDLE"
export GIT_SSL_CAINFO="$CA_BUNDLE"
export CURL_CA_BUNDLE="$CA_BUNDLE"

GITHUB_PAT=$(cat "$GITHUB_PAT_FILE")

# Decode a JWT's base64url-encoded payload to JSON on stdout.
decode_jwt_payload() {
  local jwt="$1" p
  p=$(printf '%s' "$jwt" | cut -d. -f2 | tr '_-' '/+')
  case $((${#p} % 4)) in
    2) p="${p}==" ;;
    3) p="${p}=" ;;
  esac
  printf '%s' "$p" | base64 -d
}

# --- Sparse-clone (just $SOPS_CONFIG + every token's sops_file) ---------
mkdir /tmp/repo
cd /tmp/repo
git init -q
git remote add origin "https://x-access-token:${GITHUB_PAT}@github.com/${GITHUB_REPO}.git"
git config core.sparseCheckout true
{
  echo "$SOPS_CONFIG"
  jq -r '.tokens[].sops_file' "$CONFIG_FILE"
} >.git/info/sparse-checkout
git fetch -q --depth=1 --no-tags origin devel
git checkout -q FETCH_HEAD

# --- Process each token in the config -----------------------------------
ROTATED=()
TOKEN_COUNT=$(jq '.tokens | length' "$CONFIG_FILE")
for ((i = 0; i < TOKEN_COUNT; i++)); do
  NAME=$(jq -r ".tokens[$i].name" "$CONFIG_FILE")
  SOPS_FILE=$(jq -r ".tokens[$i].sops_file" "$CONFIG_FILE")
  SUB=$(jq -r ".tokens[$i].sub" "$CONFIG_FILE")
  VALIDITY=$(jq -r ".tokens[$i].validity" "$CONFIG_FILE")

  # Freshness check on existing SOPS file (no decryption needed).
  if [ -f "$SOPS_FILE" ]; then
    EXISTING_EXPIRES=$(sed -n 's/^expires_unencrypted:[[:space:]]*//p' "$SOPS_FILE" | head -n1 | tr -d '"')
    if [ -n "$EXISTING_EXPIRES" ]; then
      EXPIRES_TS=$(date -u -d "$EXISTING_EXPIRES" +%s)
      NOW_TS=$(date +%s)
      REMAINING_H=$(((EXPIRES_TS - NOW_TS) / 3600))
      if [ "$REMAINING_H" -gt "$ROTATE_BELOW_HOURS" ]; then
        echo "${NAME}: ${REMAINING_H}h remaining > ${ROTATE_BELOW_HOURS}h threshold; skipping"
        continue
      fi
      echo "${NAME}: ${REMAINING_H}h remaining; rotating"
    else
      echo "${NAME}: existing $SOPS_FILE has no expires_unencrypted; rotating to populate"
    fi
  else
    echo "${NAME}: no existing $SOPS_FILE on devel; bootstrapping"
  fi

  # Build atticadm flag list from the per-token pull/push arrays.
  declare -a ATTICADM_ARGS
  ATTICADM_ARGS=(--sub "$SUB" --validity "$VALIDITY")
  while IFS= read -r p; do
    [ -n "$p" ] && ATTICADM_ARGS+=(--pull "$p")
  done < <(jq -r ".tokens[$i].pull[]" "$CONFIG_FILE")
  while IFS= read -r p; do
    [ -n "$p" ] && ATTICADM_ARGS+=(--push "$p")
  done < <(jq -r ".tokens[$i].push[]" "$CONFIG_FILE")

  # Mint the JWT inside the attic pod. atticadm needs an explicit -f
  # config (without it, EACCES on default config path).
  JWT=$(kubectl -n "$ATTIC_NAMESPACE" exec "$ATTIC_DEPLOYMENT" -- \
    atticadm -f /config/server.toml make-token "${ATTICADM_ARGS[@]}" | tr -d '[:space:]')

  if [ -z "$JWT" ]; then
    echo "ERROR: ${NAME}: atticadm make-token returned empty output" >&2
    exit 1
  fi

  PAYLOAD=$(decode_jwt_payload "$JWT")
  if ! EXP_TS=$(printf '%s' "$PAYLOAD" | jq -er '.exp'); then
    echo "ERROR: ${NAME}: JWT payload missing exp claim" >&2
    echo "payload: $PAYLOAD" >&2
    exit 1
  fi
  EXPIRES_ISO=$(date -u -d "@${EXP_TS}" +%Y-%m-%dT%H:%M:%SZ)

  # Write + encrypt the SOPS file.
  mkdir -p "$(dirname "$SOPS_FILE")"
  cat >"$SOPS_FILE" <<EOF
expires_unencrypted: "$EXPIRES_ISO"
${TOKEN_FIELD_NAME}: $JWT
EOF
  sops encrypt --in-place "$SOPS_FILE"

  ROTATED+=("$NAME")
done

# --- One combined commit per cycle ---------------------------------------
if [ ${#ROTATED[@]} -eq 0 ]; then
  echo "No rotations needed this cycle."
  exit 0
fi

git config user.name "$GIT_AUTHOR_NAME"
git config user.email "$GIT_AUTHOR_EMAIL"

# Stage exactly the SOPS files declared in the config (no `git add -A`).
while IFS= read -r path; do
  git add -- "$path"
done < <(jq -r '.tokens[].sops_file' "$CONFIG_FILE")

if git diff --cached --quiet; then
  echo "Tokens minted but SOPS files unchanged on disk (unexpected)."
  exit 0
fi

NAMES_JOINED=$(printf '%s, ' "${ROTATED[@]}" | sed 's/, $//')
git commit -q -m "chore: rotate attic JWTs ($(date -I)): ${NAMES_JOINED}"
git push -q origin HEAD:devel
echo "Rotated: ${NAMES_JOINED}"
