#!/usr/bin/env bash
# Write a kubeconfig for the claude-sandbox cluster to a given path.
# Decrypts the bearer token directly from secrets/claude-web-k8s-token.yaml via SOPS.
#
# Usage: kube_from_sops.sh <output-path>
#
# Requires: SOPS_AGE_KEY in env (or age keyfile). Does NOT require the token
# to be pre-set in the environment.
#
# Used by:
#   - devinfra/claude/claude-sandbox-kubectl-mcp.sh  (writes to a temp file, subprocess + trap)
#   - devinfra/secrets/web_env.sh                    (writes to ~/.kube/config as a side effect)
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $(basename "$0") <output-path>" >&2
  exit 1
fi

OUTPUT="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

token="$(sops -d --extract '["k8s_token"]' "$REPO_ROOT/secrets/claude-web-k8s-token.yaml")"

mkdir -p "$(dirname "$OUTPUT")"
cat >"$OUTPUT" <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://api.allegedly.works:16443
  name: claude-sandbox
contexts:
- context:
    cluster: claude-sandbox
    namespace: claude-sandbox
    user: claude-code-web
  name: claude-code-web
current-context: claude-code-web
users:
- name: claude-code-web
  user:
    token: ${token}
EOF
chmod 600 "$OUTPUT"
