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

# The former shared vault was deleted at Anthropic on 2026-09-30. Do not read the
# stale haku-cloud-agent-ids Secret: supply the ID of a newly provisioned vault
# that already contains the required MCP credentials before creating any objects.
: "${HAKU_MANAGED_AGENT_VAULT_ID:?Set this to a freshly provisioned Anthropic vault ID with the required MCP credentials}"
VAULT_ID="$HAKU_MANAGED_AGENT_VAULT_ID"

ENV_ID=$(ant beta:environments create --transform id -r <"$here/haku.environment.yaml")
echo "environment: $ENV_ID"
echo "  -> generate its environment key in the Console (Environments -> haku-selfhosted"
echo "     -> 'Generate environment key') and store it as the ANTHROPIC_ENVIRONMENT_KEY"
echo "     secret on the haku-managed-agent Deployment (it is never created via the API)."

AGENT_ID=$(ant beta:agents create --transform id -r <"$here/haku.agent.yaml")
echo "agent: $AGENT_ID"

echo "vault: $VAULT_ID"

# Scheduled deployment = the wake trigger (one fresh session per fire).
DEPL_ID=$(ant beta:deployments create \
  --agent "$AGENT_ID" --environment-id "$ENV_ID" --vault-id "$VAULT_ID" \
  --transform id -r <"$here/haku.deployment.yaml")
echo "deployment: $DEPL_ID"
