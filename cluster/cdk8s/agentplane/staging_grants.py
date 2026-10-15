"""Manifest-side Kubernetes grants for staging PolicySets and binding delegation.

These definitions shape cluster resources. They are not part of the Agentplane app's runtime
configuration, which only needs the names of the PolicySets it assigns.
"""

from __future__ import annotations

from agentplane.sandboxes.kubernetes_grants import KubernetesGrant
from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane.sandbox_pod import TOOL_CONFIG_GRANT, tool_config_grant


def catalog(namespace: str) -> dict[str, KubernetesGrant]:
    """Return the complete manifest-side grant catalog for one staging namespace."""
    return {TOOL_CONFIG_GRANT: tool_config_grant(namespace), **agent_access_profiles.catalog()}
