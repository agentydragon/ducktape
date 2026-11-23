"""Logical Agentplane databases and the credential Secrets their clients consume."""

from enum import StrEnum

from cdk8s_plus_34 import EnvValue
from constructs import Construct

from cluster.cdk8s.secret_ref import SecretRef


def role_secret_name(role: str) -> str:
    return f"postgres-{role.replace('_', '-')}"


class Database(StrEnum):
    APP = "app"
    ACTIONS = "actions"
    EGRESS = "egress"
    MCP_EDGE = "mcp_edge"
    NOTIFICATIONS = "notifications"
    HISTORY = "sandbox_service"  # Persisted History data lives under this legacy name.
    SESSION_DISPATCHER = "session_dispatcher"
    CENTRAL_SAMPLER = "central_sampler"

    @property
    def secret_name(self) -> str:
        return role_secret_name(self)

    def url(self, scope: Construct, *, namespace: str, key: str = "uri") -> EnvValue:
        return SecretRef(namespace=namespace, name=self.secret_name).key(key).env_value(scope, f"{self}-database-url")
