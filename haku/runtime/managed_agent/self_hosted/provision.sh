#!/usr/bin/env bash
# Provision Haku's Managed Agents control plane via the `ant` CLI.
#
# Run from OUTSIDE the worker host (operator laptop / CI) authenticated with an
# org-scoped ANTHROPIC_API_KEY (or `ant auth login` profile) — NEVER on the
# worker pod, which holds only the environment key so agent tool calls can't
# reach the control plane. First-time create only; iterate later with
# `ant beta:agents update --agent-id <id> --version <n> < haku.agent.yaml`.
#
# Self-hosted is provisioned imperatively (here), NOT via the claude-managed-agents
# tofu provider — the provider forces a `networking` block the API rejects for
# self_hosted. See README.md "Why this is provisioned imperatively, not Terraform".
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_ID=$(ant beta:environments create --transform id -r <"$here/haku.environment.yaml")
echo "environment: $ENV_ID"
echo "  -> generate its environment key in the Console (Environments -> haku-selfhosted"
echo "     -> 'Generate environment key') and store it as the ANTHROPIC_ENVIRONMENT_KEY"
echo "     secret on the haku-worker Deployment (it is never created via the API)."

AGENT_ID=$(ant beta:agents create --transform id -r <"$here/haku.agent.yaml")
echo "agent: $AGENT_ID"

# Vault for MCP credentials. Each static bearer is read from the owning namespace
# and piped via stdin, never argv. Anthropic injects it at egress; the pod never
# sees it.
VAULT_ID=$(ant beta:vaults create --display-name haku-mcp --transform id -r)
echo "vault: $VAULT_ID"

# tana-mcp-ro: read-only Tana facade. Bearer already reflected into haku-sandbox.
TANA_TOKEN=$(kubectl -n haku-sandbox get secret haku-tana-ro-token -o jsonpath='{.data.token}' | base64 -d)
ant beta:vaults:credentials create --vault-id "$VAULT_ID" >/dev/null <<YAML
display_name: tana-mcp-ro (read-only)
auth:
  type: static_bearer
  mcp_server_url: https://tana-mcp-ro.allegedly.works/mcp
  token: ${TANA_TOKEN}
YAML
echo "  -> tana-mcp-ro credential stored in vault $VAULT_ID"

# gmail-labeling: Haku's ONE sanctioned world-write, bounded server-side to
# labels under `haku/`. The MCP's static client bearer lives in its own namespace.
GMAIL_TOKEN=$(kubectl -n gmail-labeling get secret haku-gmail-labeling-token -o jsonpath='{.data.token}' | base64 -d)
ant beta:vaults:credentials create --vault-id "$VAULT_ID" >/dev/null <<YAML
display_name: gmail-labeling (managed labels under haku/)
auth:
  type: static_bearer
  mcp_server_url: https://gmail-labeling.allegedly.works/mcp
  token: ${GMAIL_TOKEN}
YAML
echo "  -> gmail-labeling credential stored in vault $VAULT_ID"

# Scheduled deployment = the wake trigger (one fresh session per fire).
DEPL_ID=$(ant beta:deployments create \
  --agent "$AGENT_ID" --environment-id "$ENV_ID" --vault-id "$VAULT_ID" \
  --transform id -r <"$here/haku.deployment.yaml")
echo "deployment: $DEPL_ID"
