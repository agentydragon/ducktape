"""Fresh credentials for an individual disposable k3s stack."""

import secrets
import string

import bcrypt

from agentplane.x.k3s_env.fixture_credentials import Credentials, McpCredentials


def _password(length: int = 40) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def credentials() -> Credentials:
    return Credentials(
        postgres_bootstrap=_password(),
        postgres_app=_password(),
        postgres_actions=_password(),
        postgres_egress=_password(),
        postgres_mcp_edge=_password(),
        postgres_notifications=_password(),
        postgres_sandbox_service=_password(),
        postgres_session_dispatcher=_password(),
        app_client_secret=_password(),
        app_session_secret=_password(64),
        actions_client_secret=_password(),
    )


def mcp_credentials() -> McpCredentials:
    password = _password()
    return McpCredentials(
        edge_client_secret=_password(),
        operator_password=password,
        operator_password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10, prefix=b"2a")).decode(),
    )
