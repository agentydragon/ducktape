#!/usr/bin/env bash
# Startup script for persistent GitHub Actions runner.
# Generates a registration token from the mounted GitHub App credentials,
# configures the runner, and runs it in non-ephemeral mode (picks up
# multiple jobs on the same process, preserving Bazel JVM/Skyframe cache).
set -euo pipefail

REPO_URL="https://github.com/agentydragon/ducktape"
APP_ID=$(cat /etc/github-app/github_app_id)
INSTALL_ID=$(cat /etc/github-app/github_app_installation_id)
PRIVATE_KEY=/etc/github-app/github_app_private_key

# --- Generate GitHub App JWT (RS256, valid 10 min) ---
b64url() { base64 -w0 | tr '+/' '-_' | tr -d '='; }

HEADER=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
NOW=$(date +%s)
PAYLOAD=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((NOW - 60))" "$((NOW + 600))" "$APP_ID" | b64url)
SIGNATURE=$(printf '%s.%s' "$HEADER" "$PAYLOAD" | openssl dgst -sha256 -sign "$PRIVATE_KEY" | b64url)
JWT="${HEADER}.${PAYLOAD}.${SIGNATURE}"

# --- Get installation access token ---
ACCESS_TOKEN=$(curl -sf -X POST \
  -H "Authorization: Bearer ${JWT}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/app/installations/${INSTALL_ID}/access_tokens" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# --- Get runner registration token ---
REG_TOKEN=$(curl -sf -X POST \
  -H "Authorization: token ${ACCESS_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/agentydragon/ducktape/actions/runners/registration-token" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# --- Configure runner (idempotent with --replace) ---
./config.sh \
  --url "$REPO_URL" \
  --token "$REG_TOKEN" \
  --name "$HOSTNAME" \
  --labels ducktape-runners \
  --replace \
  --unattended \
  --disableupdate

# --- Run (non-ephemeral: stays alive across jobs) ---
exec ./run.sh
