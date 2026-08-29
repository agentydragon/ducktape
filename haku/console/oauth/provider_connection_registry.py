"""Well-known external OAuth providers the console connects on an Operator's behalf.

Each Operator links deploy-named accounts backed by configured instances of one of these provider
kinds (Google today); the
console stores each refresh token and self-refreshes access in-process, replacing Airlock's
brokered token. Unlike ``mcp/operator_oauth`` — which discovers a remote MCP
server's authorization server and registers a per-Operator DCR client at connect time —
these are fixed, pre-registered OAuth clients. Deploy config owns the named client instances and
their secret environment references; this module holds only protocol-level provider metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderConnectionKind(StrEnum):
    GOOGLE = "google"


@dataclass(frozen=True, slots=True)
class ProviderConnectionDescriptor:
    display_name: str
    authorize_url: str
    token_url: str
    # Extra authorize-request params. Google needs access_type=offline + prompt=consent to
    # return a refresh token (and to keep returning one on reconnect).
    extra_auth_params: tuple[tuple[str, str], ...]
    # How the client authenticates to the token endpoint. Google accepts the secret in the
    # POST body (client_secret_post), matching Airlock's GenericOAuth2Provider.
    token_endpoint_auth_method: str = "client_secret_post"


PROVIDER_DESCRIPTORS: dict[ProviderConnectionKind, ProviderConnectionDescriptor] = {
    ProviderConnectionKind.GOOGLE: ProviderConnectionDescriptor(
        display_name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        extra_auth_params=(("access_type", "offline"), ("prompt", "consent")),
    )
}
