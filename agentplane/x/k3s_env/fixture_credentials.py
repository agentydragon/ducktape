"""Credential slots in cached fixture manifests, filled separately for each running stack."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    postgres_bootstrap: str
    postgres_app: str
    postgres_actions: str
    postgres_egress: str
    postgres_mcp_edge: str
    postgres_notifications: str
    postgres_sandbox_service: str
    postgres_session_dispatcher: str
    app_client_secret: str
    app_session_secret: str
    actions_client_secret: str


@dataclass(frozen=True)
class McpCredentials:
    edge_client_secret: str
    operator_password: str
    operator_password_hash: str


PLACEHOLDERS = Credentials(
    postgres_bootstrap="k3s-env-placeholder-postgres-bootstrap",
    postgres_app="k3s-env-placeholder-postgres-app",
    postgres_actions="k3s-env-placeholder-postgres-actions",
    postgres_egress="k3s-env-placeholder-postgres-egress",
    postgres_mcp_edge="k3s-env-placeholder-postgres-mcp-edge",
    postgres_notifications="k3s-env-placeholder-postgres-notifications",
    postgres_sandbox_service="k3s-env-placeholder-postgres-sandbox-service",
    postgres_session_dispatcher="k3s-env-placeholder-postgres-session-dispatcher",
    app_client_secret="k3s-env-placeholder-app-client-secret",
    app_session_secret="k3s-env-placeholder-app-session-secret",
    actions_client_secret="k3s-env-placeholder-actions-client-secret",
)
MCP_PLACEHOLDERS = McpCredentials(
    edge_client_secret="k3s-env-placeholder-edge-client-secret",
    operator_password="k3s-env-placeholder-operator-password",
    operator_password_hash="k3s-env-placeholder-operator-bcrypt",
)
