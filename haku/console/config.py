"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from mcp_infra.authentik_auth.config import AuthentikAuthConfig
from mcp_infra.exec.models import MAX_EXEC_TIMEOUT_MS
from mcp_infra.persistence import PostgresPersistence

# Both URLs are built from the routine (trigger) id, so only the id + token are
# configured. The fire endpoint performs the launch; the claude.ai page is the
# operator-facing deep-link to review past runs (there's no runs-listing API).
_FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{id}/fire"
_PAGE_URL = "https://claude.ai/code/routines/{id}"

# Public path of the MCP resource. The outer console origin is the shared source of
# truth; MCP OAuth derives its issuer/callback URLs from this path instead of accepting a second,
# independently configurable public URL that can drift away from the actual mount.
MCP_PATH = "/mcp"


def _postgres_connection_identity(raw_url: str) -> tuple[object, ...]:
    """Return the authority-bearing parts of a Postgres URL, independent of its driver."""
    try:
        url = make_url(raw_url)
    except ArgumentError as error:
        raise ValueError("database URL must be a valid PostgreSQL URL") from error
    if url.get_backend_name() != "postgresql":
        raise ValueError("database URL must use PostgreSQL")
    normalized_query = tuple(sorted((key, tuple(values)) for key, values in url.normalized_query.items()))
    return (
        url.username,
        url.password,
        url.host.lower() if url.host is not None else None,
        url.port or 5432,
        url.database,
        normalized_query,
    )


class LaunchRoutineConfig(BaseModel):
    """The `launch-routine` capability: the routine (trigger) id plus the bearer that
    authorizes firing it. Both come from the deployment env / `haku-routine-launch-token`
    secret; set together or not at all (the capability is disabled when unset). Only the
    token is secret, and it lives only in the haku-console namespace — Haku can't read
    it. The fire and page URLs are derived from the id."""

    routine_id: str
    token: SecretStr

    @property
    def fire_url(self) -> str:
        return _FIRE_URL.format(id=self.routine_id)

    @property
    def page_url(self) -> str:
        return _PAGE_URL.format(id=self.routine_id)


class OperatorOidcConfig(BaseModel):
    """Authentik OIDC relying-party config for operator **browser** login.

    The console authenticates the operator's browser itself (Authentik authorization-code flow →
    signed session cookie), replacing the retired Authentik proxy outpost. Agent access to `/mcp`
    uses its own MultiAuth and is unaffected.
    Reads `HAKU_CONSOLE_OPERATOR_OIDC__{ISSUER,CLIENT_ID,CLIENT_SECRET,SESSION_SECRET}`. The redirect
    URI is built from the top-level `public_base_url` + `/auth/callback`.

    Authorization is delegated to Authentik: the application's access-policy binding (a single-user
    group in the deploy) decides *who* may complete the flow — the console only requires a valid
    session, exactly as the proxy outpost does today (no in-app username allowlist).
    """

    issuer: str  # per-provider issuer, e.g. https://auth.allegedly.works/application/o/haku-console/
    client_id: str
    client_secret: SecretStr
    session_secret: SecretStr  # signs the session cookie

    @property
    def server_metadata_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


class OperatorIdentityConfig(BaseModel):
    """The stable Authentik user-id namespace shared by Haku's OIDC clients.

    Haku deliberately does not equate bare OIDC ``sub`` values. A subject becomes a stable external
    user key only when it came from one of the exact configured browser/MCP issuers, both of which
    are provisioned with Authentik ``sub_mode=user_id``. ``trust_domain`` names that deployment
    contract; the issuer allowlist itself is derived from the two role-specific OIDC settings so it
    cannot drift from the clients actually doing verification.
    """

    model_config = ConfigDict(frozen=True)

    trust_domain: str

    @field_validator("trust_domain")
    @classmethod
    def _nonempty_trust_domain(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("trust_domain must not be empty")
        return value


class McpOAuthConfig(BaseModel):
    """Credentials for Haku's agent-facing OAuth authorization-server proxy.

    Unlike the reusable ``AuthentikAuthConfig``, this Haku-specific config deliberately has no
    ``public_base_url``. The public MCP URL is always ``Settings.public_base_url`` + ``/mcp``, so
    the issuer, DCR endpoints, and callback cannot be configured for a different mount. Operator
    login uses separate credentials and session state; only the canonical origin is shared. OAuth
    always includes a shared Postgres client-state store. Haku already requires Postgres for its
    domain state, and keeping the OAuth state in that same database lets an authority-changing
    migration invalidate every old client/token family atomically. Process-local file and Valkey
    persistence remain valid generic MCP infrastructure choices, but are not Haku deployment modes.
    """

    model_config = ConfigDict(frozen=True)

    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    persistence: PostgresPersistence

    def as_authentik_auth_config(self, *, public_base_url: str) -> AuthentikAuthConfig:
        return AuthentikAuthConfig(
            oidc_issuer=self.oidc_issuer,
            oidc_client_id=self.oidc_client_id,
            oidc_client_secret=self.oidc_client_secret.get_secret_value(),
            public_base_url=f"{public_base_url.rstrip('/')}{MCP_PATH}",
        )


class ProviderOAuthClientConfig(BaseModel):
    """A pre-registered OAuth client for a per-Operator provider connection.

    The console runs the authorization-code + PKCE flow with this client and self-refreshes
    the resulting per-Operator tokens. The secret lives only in the haku-console namespace
    (Haku cannot read it) and is never persisted to the database.
    """

    client_id: str
    client_secret: SecretStr


class HostexecHostConfig(BaseModel):
    """One in-scope host for the `hostexec` in-process server.

    `exec_url` is how the console reaches this host's `hostexecd` (the k8s node hostname on the
    cluster pod network, or a Service DNS name). `audience_client_id` is the Authentik client_id of
    the host's `hostexec-<host>` provider — the audience the operator's token is exchanged for.
    """

    exec_url: str
    audience_client_id: str


class HostexecConfig(BaseModel):
    """The `hostexec` in-process server: the in-scope hosts and the token-exchange scope.

    The Authentik token endpoint is derived from the operator OIDC issuer at composition (the same
    Authentik that authenticated the operator mints the per-host token from their identity), so it
    is not configured here. Unset on `Settings` → the server is not offered.
    """

    hosts: dict[str, HostexecHostConfig]
    # Scopes requested on the per-host exchange. `groups` is required — the per-host provider's
    # `groups` scope mapping emits the operator's `hostexec-*` groups, which hostexecd checks for
    # `hostexec-<run_as>-<host>`; `openid` carries `sub` for the audit log. Configurable in case the
    # Authentik scope mapping is named differently.
    exchange_scope: str = "openid groups"
    # hostexecd responds only once the command finishes or its own timeout_ms fires (capped at
    # MAX_EXEC_TIMEOUT_MS), so the HTTP wait must outlast the longest command plus margin — otherwise
    # a slow-but-legitimate command is cut off by an HTTP timeout instead of returning its result.
    request_timeout: float = MAX_EXEC_TIMEOUT_MS / 1000 + 30


class Settings(BaseSettings):
    # env_nested_delimiter so launch_routine.{routine_id,token} read from
    # HAKU_CONSOLE_LAUNCH_ROUTINE__{ROUTINE_ID,TOKEN}.
    model_config = SettingsConfigDict(env_prefix="HAKU_CONSOLE_", env_nested_delimiter="__")

    # Optional directory holding the built React SPA (index.html + assets), served
    # same-origin by FastAPI for direct local/dev fallback. Production leaves this
    # unset and serves the SPA from the haku-console-static nginx image.
    static_dir: Path | None = None

    # Capability tier. launch_routine enables POST /api/capabilities/launch-routine
    # (None → the capability returns 503). csrf_secret signs the double-submit CSRF
    # tokens that gate the capability tier; when unset, create_app generates an
    # ephemeral one at startup (fine for the single-replica console — a restart just
    # makes the SPA refetch its token).
    launch_routine: LaunchRoutineConfig | None = None
    csrf_secret: SecretStr | None = None

    # The Authentik-gated origin of Haku's own UI service (runs in haku-sandbox), which the
    # console frames full-page as a sandboxed cross-origin iframe; the CSP allows framing it
    # plus Authentik's origin for the in-iframe SSO redirect. Required — framing haku-ui is
    # the console's whole job; set it to `about:blank` if there is genuinely no UI to frame.
    # The console never renders Haku's UI itself. See docs/containment.md.
    haku_ui_url: str
    auth_origin: str = "https://auth.allegedly.works"
    # Canonical public console origin used for OAuth redirects, secure-cookie policy, and
    # WebSocket origin checks. Required: there is no unauthenticated runtime mode.
    public_base_url: str

    # Optional YAML file for deploy-time console configuration that does not belong
    # in env vars. Secret values stay in env/Kubernetes Secret references; this file
    # names the connected MCP servers, the env-backed credential slot each uses, and the
    # static machine `agents` (each an agent id + env-referenced bearer + operator subject).
    config_file: Path | None = None

    # Shared haku-console Postgres database. Required: it holds the MCP approval audit/result
    # ledger and the operator OAuth token store — the console does not run without them. Both
    # stores are always constructed; migrations are applied once at startup (see app.main).
    database_url: SecretStr

    # Pre-registered Google OAuth client backing per-Operator Google connections (the `gmail`
    # and `google_calendar` in-process servers). Reads HAKU_CONSOLE_GOOGLE_CLIENT__{CLIENT_ID,
    # CLIENT_SECRET}. Unset → no Google connection is offered (both servers stay degraded). This
    # replaces Airlock's brokered `haku_console_google` token: the console now holds the client and
    # each Operator's refresh token itself. See haku/console/provider_connection.py.
    google_client: ProviderOAuthClientConfig | None = None
    # Namespace whose Gmail label mutations Haku may auto-approve. labels_list is
    # wholesale because Haku already has standing Gmail read authority.
    gmail_auto_approve_label_prefix: str = "haku/"

    # Operator-facing console origin (e.g. https://haku.allegedly.works) used to build deep links
    # to a specific tool call in the SPA, returned in the MCP server's promise/read-tool results.
    # Unset → no link is included.
    ui_base_url: str | None = None

    # The `hostexec` in-process server: run a shell command on an operator machine (wyrm2/rugged)
    # under the operator's own Authentik authority. Set via `HAKU_CONSOLE_HOSTEXEC` as a JSON object
    # (the host map + exchange scope). Unset → the server is not offered, no offline_access is
    # requested, and no operator Authentik token is persisted (nothing would read it).
    hostexec: HostexecConfig | None = None

    # OAuth for Agent admission to the MCP server: an Authentik-backed OIDCProxy handling MCP OAuth
    # dance (DCR + PKCE) for claude.ai / the `claude` CLI, composed with the static agent bearer via
    # MultiAuth. Reads HAKU_CONSOLE_MCP_OAUTH__{OIDC_ISSUER,OIDC_CLIENT_ID,OIDC_CLIENT_SECRET} plus
    # HAKU_CONSOLE_MCP_OAUTH__PERSISTENCE__*; its public URL is derived from top-level
    # public_base_url + MCP_PATH. Unset → the static bearer is the only accepted credential (no
    # OAuth, and therefore no OAuth store).
    mcp_oauth: McpOAuthConfig | None = None
    # Operator browser login (Authentik OIDC), replacing the proxy outpost. Required in every
    # runtime, including development; tests use the repo's hermetic OIDC fixture.
    operator_oidc: OperatorOidcConfig
    # Canonical Operator identity trust contract. Required in every runtime; see
    # ``OperatorIdentityConfig`` for why this is distinct from either OIDC client.
    operator_identity: OperatorIdentityConfig

    @model_validator(mode="after")
    def _operator_auth_requires_canonical_public_origin(self) -> Self:
        parsed = urlsplit(self.public_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public_base_url must be a canonical http(s) origin without a path, query, or fragment")
        return self

    @model_validator(mode="after")
    def _mcp_oauth_state_must_share_the_owned_database(self) -> Self:
        if self.mcp_oauth is None:
            return self
        database_identity = _postgres_connection_identity(self.database_url.get_secret_value())
        oauth_identity = _postgres_connection_identity(self.mcp_oauth.persistence.url)
        if database_identity != oauth_identity:
            raise ValueError(
                "mcp_oauth.persistence must use the same Postgres host, port, database, "
                "credentials, and options as database_url"
            )
        return self
