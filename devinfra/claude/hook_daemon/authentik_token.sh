#!/usr/bin/env bash
# Fetches the Authentik service account API token for Claude sessions.
# Polls for kubectl availability so it works on web (where kubeconfig is
# set up by a concurrent background task) and CLI (kubectl ready immediately).
# On success, writes AUTHENTIK_API_TOKEN to $DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/authentik_token.sh
# (sourced from env_exports on each tool call) and prints usage context for Claude.
set -uo pipefail

for i in $(seq 10); do
  if kubectl cluster-info --request-timeout=3s >/dev/null 2>&1; then break; fi
  if [ "$i" = "10" ]; then
    echo "WARNING: kubectl not reachable; Authentik token not loaded"
    exit 0
  fi
  sleep 2
done

token=$(kubectl get secret -n claude-sandbox claude-authentik-api-token \
  -o "jsonpath={.data.token}" 2>/dev/null | base64 -d 2>/dev/null) || true
url=$(kubectl get secret -n claude-sandbox claude-authentik-api-token \
  -o "jsonpath={.data.url}" 2>/dev/null | base64 -d 2>/dev/null) || true

if [ -z "${token:-}" ]; then
  echo "WARNING: Authentik API token not found (secret missing or RBAC not applied yet)"
  exit 0
fi

echo "export AUTHENTIK_API_TOKEN='${token}'" >"$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/authentik_token.sh"
echo "AUTHENTIK_API_TOKEN loaded — claude-service-account at ${url}"
echo "Permissions: audit log, apps, proxy providers, users, groups, sessions, outpost health, flows, policies, system tasks, blueprints."
echo "NOT available: OAuth2 provider client secrets, access/refresh tokens, certificate keys."
echo "Usage: curl -sH 'Authorization: Bearer \$AUTHENTIK_API_TOKEN' ${url}/api/v3/<endpoint>/"
echo "Key endpoints: /api/v3/events/events/ (audit log), /api/v3/core/applications/, /api/v3/core/users/, /api/v3/outposts/instances/ (outpost health), /api/v3/admin/system_tasks/, /api/v3/flows/instances/"
