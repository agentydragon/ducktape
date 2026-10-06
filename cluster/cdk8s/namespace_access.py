"""Reviewed namespace diagnostics policy, shared by static and managed agents.

An absent namespace grants nothing. LOGS includes metadata. Credential reads and
service-specific exceptions remain separate grants in agent_access_profiles.py.
"""

from enum import StrEnum


class AgentReadable(StrEnum):
    """Descriptive namespace label keys; LOGS includes METADATA."""

    METADATA = "rbac.ducktape.io/agent-readable-metadata"
    LOGS = "rbac.ducktape.io/agent-readable-logs"


# `plaid-mcp` is intentionally absent: it contains Plaid-linked private data and
# the Finance-only spend policy, so access uses narrower named roles instead.
NAMESPACE_DIAGNOSTICS = {
    "activitywatch": AgentReadable.LOGS,
    "agent-sandbox-system": AgentReadable.METADATA,
    "agentplane-index": AgentReadable.LOGS,
    "agentplane-staging": AgentReadable.LOGS,
    "agentplane-testing": AgentReadable.LOGS,
    "airlock": AgentReadable.LOGS,
    "authentik": AgentReadable.LOGS,
    "cert-manager": AgentReadable.LOGS,
    "cli-proxy-api": AgentReadable.LOGS,
    "clickhouse": AgentReadable.LOGS,
    "cnpg-system": AgentReadable.LOGS,
    "flux-system": AgentReadable.LOGS,
    "gatus": AgentReadable.LOGS,
    "grocy-sf": AgentReadable.LOGS,
    "grocy-vallejo": AgentReadable.LOGS,
    "haku-ci": AgentReadable.LOGS,
    "litellm": AgentReadable.LOGS,
    "local-path-storage": AgentReadable.LOGS,
    "loki": AgentReadable.LOGS,
    "monitoring": AgentReadable.LOGS,
    "nix-cache": AgentReadable.METADATA,
    "node-feature-discovery": AgentReadable.LOGS,
    "nvidia-device-plugin": AgentReadable.LOGS,
    "oci-cache": AgentReadable.LOGS,
    "openebs": AgentReadable.LOGS,
    "proxmox-proxy": AgentReadable.LOGS,
    "public-coder-agent": AgentReadable.METADATA,
    "study-casino": AgentReadable.LOGS,
    "tana-mcp": AgentReadable.LOGS,
    "vm-images-publisher": AgentReadable.METADATA,
}
