"""Well-known external OAuth providers the console connects on an Operator's behalf.

Each Operator links their own account for one of these providers (Google today); the
console stores the refresh token and self-refreshes the access token in-process, replacing
Airlock's brokered token. Unlike ``mcp_operator_oauth`` — which discovers a remote MCP
server's authorization server and registers a per-Operator DCR client at connect time —
these are fixed, pre-registered OAuth clients described statically here. The client
``client_id``/``client_secret`` are injected from ``Settings`` at runtime and are never
stored in the database; this module holds only non-secret provider metadata.
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
    scopes: tuple[str, ...]
    # Extra authorize-request params. Google needs access_type=offline + prompt=consent to
    # return a refresh token (and to keep returning one on reconnect).
    extra_auth_params: tuple[tuple[str, str], ...]
    # How the client authenticates to the token endpoint. Google accepts the secret in the
    # POST body (client_secret_post), matching Airlock's GenericOAuth2Provider.
    token_endpoint_auth_method: str = "client_secret_post"

    @property
    def scope(self) -> str:
        return " ".join(self.scopes)


# The Google scope set carries calendar-event + Gmail writes plus a broad read-only surface, giving
# the per-Operator token the same capabilities as the retired Airlock ``haku_console_google`` grant it
# replaced. The gmail/google_calendar tools request narrower per-service scope subsets; the linked
# token already holds whatever was granted here.
_GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
)


PROVIDER_DESCRIPTORS: dict[ProviderConnectionKind, ProviderConnectionDescriptor] = {
    ProviderConnectionKind.GOOGLE: ProviderConnectionDescriptor(
        display_name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=_GOOGLE_SCOPES,
        extra_auth_params=(("access_type", "offline"), ("prompt", "consent")),
    )
}
