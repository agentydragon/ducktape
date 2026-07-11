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

# Shared vault: the vault + ALL MCP credentials (kubectl-machine, grocy-sf, tana-ro,
# native MCP credentials are TF-managed by the cloud agent module (tf/gitops/haku-cloud-agent)
# and published to the haku-cloud-agent-ids Secret. Both agents reference the same
# vault, so this agent no longer creates its own — read the shared ID. (The vault +
# creds are the one part of the self-hosted agent that IS declarative; only the
# environment/agent/deployment stay imperative, since the provider can't do self_hosted.)
VAULT_ID=$(kubectl -n flux-system get secret haku-cloud-agent-ids -o jsonpath='{.data.vault_id}' | base64 -d)
echo "shared vault: $VAULT_ID"

# Scheduled deployment = the wake trigger (one fresh session per fire).
DEPL_ID=$(ant beta:deployments create \
  --agent "$AGENT_ID" --environment-id "$ENV_ID" --vault-id "$VAULT_ID" \
  --transform id -r <"$here/haku.deployment.yaml")
echo "deployment: $DEPL_ID"
