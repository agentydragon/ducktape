"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from haku.console.chat_models import RuntimeKind
from haku.console.http_url import UncredentialedHttpUrl
from haku.console.x.codex_app_server.config import CodexAppServerImplementationConfig
from haku.recall_index.config import EmbedderConfig, RecallIndexSettings
from mcp_infra.authentik_auth.config import AuthentikAuthConfig
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

# The console's reserved SPA namespace (frontend/routing.ts's CONSOLE_ROOT_PATH). Trusted console
# pages live under it; every other path belongs to the framed haku-ui.
_CONSOLE_ROOT_PATH = "/_console"


def _proxy_environment(*, proxy_url: str, no_proxy: str, ca_bundle: str, pip: bool = False) -> dict[str, str]:
    """Build the common explicit-proxy and CA environment without alias drift."""
    environment = {
        **dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY"), proxy_url),
        "NO_PROXY": no_proxy,
        "NODE_USE_ENV_PROXY": "1",
        **dict.fromkeys(("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"), ca_bundle),
    }
    if pip:
        environment["PIP_CERT"] = ca_bundle
    return environment


def tool_call_console_url(console_base_url: str, tool_call_id: str) -> str:
    """The console URL that opens one tool call: the approvals drawer, that call expanded.

    One definition, because three things must agree on it — the link the MCP server hands an agent
    when its call becomes a promise, the deep link a push notification opens, and the SPA route
    that resolves it. They previously did not: the advertised link was built from a second,
    separately configured origin and pointed at `/tool-calls/<id>`, a path the console mirrors into
    the haku-ui frame rather than one of its own pages.
    """
    return f"{console_base_url.rstrip('/')}{_CONSOLE_ROOT_PATH}/tool-calls/{tool_call_id}"


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
    signed session cookie). Agent access to `/mcp` uses its own MultiAuth and is unaffected.
    Reads `HAKU_CONSOLE_OPERATOR_OIDC__{ISSUER,CLIENT_ID,CLIENT_SECRET,SESSION_SECRET}`. The redirect
    URI is built from the top-level `public_base_url` + `/auth/callback`.

    Authorization is delegated to Authentik: the application's access-policy binding (a single-user
    group in the deploy) decides *who* may complete the flow — the console only requires a valid
    session, with no in-app username allowlist.
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


class KubernetesAuthorizationSubject(BaseModel):
    """The fixed Kubernetes identity used for Console SubjectAccessReviews.

    This is deployment policy, not request data.  In particular, the Agent
    bearer and the proxy request cannot choose a username or group set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str = Field(min_length=1)
    groups: tuple[str, ...] = ()

    @field_validator("username")
    @classmethod
    def _nonempty_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Kubernetes SAR username must not be empty")
        return value

    @field_validator("groups")
    @classmethod
    def _distinct_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        value = tuple(group.strip() for group in value)
        if any(not group for group in value):
            raise ValueError("Kubernetes SAR groups must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("Kubernetes SAR groups must be distinct")
        return value


class KubernetesAuthorizationConfig(BaseModel):
    """Explicit opt-in for Console-side Kubernetes SAR authorization.

    ``None`` on :class:`Settings` is the production-safe default: the
    endpoint remains unavailable and the proxy fails closed until a deploy
    deliberately configures a standing Kubernetes subject.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subjects_by_access_profile: dict[str, KubernetesAuthorizationSubject]
    timeout_seconds: float = Field(default=2.0, gt=0.0)

    @field_validator("subjects_by_access_profile")
    @classmethod
    def _nonempty_profile_subjects(
        cls, value: dict[str, KubernetesAuthorizationSubject]
    ) -> dict[str, KubernetesAuthorizationSubject]:
        normalized = {profile_id.strip(): subject for profile_id, subject in value.items()}
        if not normalized or any(not profile_id for profile_id in normalized):
            raise ValueError("Kubernetes SAR subjects must name at least one non-empty access profile")
        if len(normalized) != len(value):
            raise ValueError("Kubernetes SAR access profiles must be distinct after trimming")
        return normalized


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


class MatrixConfig(BaseModel):
    """Wiring for the Matrix chat surface (<channels/matrix/SPEC.md>).

    Optional on Settings: the console must start and serve without it, because the bot
    password is reflected in from another namespace and is legitimately absent on a first
    deploy. Absent config means no sync loop, not a failed startup.
    """

    model_config = ConfigDict(frozen=True)

    homeserver: str
    user_id: str
    operator_user_id: str = Field(description="The only MXID whose room invitations are joined.")
    operator_subject: str = Field(
        description=(
            "The Authentik `sub_mode=user_id` value for `operator_user_id`, resolved once to a "
            "canonical Operator UUID. Matrix has no OIDC identity of its own, so this deploy-time "
            "pair is the whole sender-to-Operator mapping; the MXID never carries authority "
            "on its own."
        )
    )
    device_id: str = Field(
        default="haku-console",
        description="Pinned so repeated logins reuse one device instead of leaving a new one per restart.",
    )
    password: SecretStr | None = Field(
        default=None,
        description=(
            "Absent until the reflected Secret lands in this namespace — an intentional state, not a "
            "misconfiguration. The sync loop does not start without it; the console does."
        ),
    )


class RuntimeExecutionConfig(BaseModel):
    """Provider-neutral placement, session, and network wiring.

    Deliberately no prompt here: prompts belong to launchable Agents
    (`launchable_agents[].system_prompt_template`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str
    warm_pool: str
    cwd: str
    session_ttl_seconds: int = Field(ge=300, le=86400)
    https_proxy: str
    ca_bundle: str
    no_proxy: str
    mcp_url: UncredentialedHttpUrl

    def proxy_environment(self, *, pip: bool = False) -> dict[str, str]:
        return _proxy_environment(proxy_url=self.https_proxy, no_proxy=self.no_proxy, ca_bundle=self.ca_bundle, pip=pip)


class ClaudeCodeImplementationConfig(BaseModel):
    """The settings that belong specifically to the Claude CLI implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[RuntimeKind.CLAUDE_CODE] = RuntimeKind.CLAUDE_CODE
    oauth_placeholder: str


type RuntimeImplementationConfig = Annotated[
    ClaudeCodeImplementationConfig | CodexAppServerImplementationConfig, Field(discriminator="kind")
]


class RuntimeRegistrationConfig(RuntimeExecutionConfig):
    """One Agent's shared execution wiring plus its native runtime implementation."""

    agent_id: UUID
    claim_prefix: str = Field(min_length=1)
    runtime_label: str = Field(min_length=1)
    implementation: RuntimeImplementationConfig

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind(self.implementation.kind)

    def environment(self) -> dict[str, str]:
        implementation = self.implementation
        if isinstance(implementation, ClaudeCodeImplementationConfig):
            provider_environment = {"CLAUDE_CODE_OAUTH_TOKEN": implementation.oauth_placeholder}
        else:
            provider_environment = {
                "GH_PAT": implementation.github_token_placeholder,
                "GITHUB_TOKEN": implementation.github_token_placeholder,
            }
        return {
            **self.proxy_environment(pip=isinstance(implementation, CodexAppServerImplementationConfig)),
            **provider_environment,
        }


class ChatRuntimesConfig(BaseModel):
    """The closed catalog of chat-runtime implementations this deployment can launch.

    A field is an implementation kind, not an arbitrary runtime-instance id. There is exactly one
    configuration per implementation until a concrete need for several instances of one kind
    exists; adding another implementation therefore extends this model with another named field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claude_code: RuntimeRegistrationConfig
    codex_app_server: RuntimeRegistrationConfig | None = None

    @field_validator("claude_code")
    @classmethod
    def _claude_slot_accepts_only_claude(cls, value: RuntimeRegistrationConfig) -> RuntimeRegistrationConfig:
        if value.kind is not RuntimeKind.CLAUDE_CODE:
            raise ValueError("harnesses.claude_code must select the claude_code implementation")
        return value

    @field_validator("codex_app_server")
    @classmethod
    def _codex_slot_accepts_only_codex(
        cls, value: RuntimeRegistrationConfig | None
    ) -> RuntimeRegistrationConfig | None:
        if value is not None and value.kind is not RuntimeKind.CODEX_APP_SERVER:
            raise ValueError("harnesses.codex_app_server must select the codex_app_server implementation")
        return value

    @property
    def registrations(self) -> tuple[RuntimeRegistrationConfig, ...]:
        """Agent/runtime registrations represented by this closed deploy catalog."""
        return tuple(runtime for runtime in (self.claude_code, self.codex_app_server) if runtime is not None)


class WebPushConfig(BaseModel):
    """VAPID identity for Web Push notifications of pending approvals (RFC 8292).

    The keypair *is* this console's application identity to every browser push service: the SPA
    hands the public half to the browser at subscribe time and the push service binds it to that
    subscription, so it verifies the signature on each push against the key recorded then.
    Rotating the key therefore invalidates every stored subscription and each device must
    re-subscribe — `POST /api/push/subscriptions` overwrites by endpoint, so re-subscribing is
    the only recovery.

    Only the private key is configured; the public half is derived from it, because two
    independently-set values that must agree is a class of outage worth designing out. `subject`
    is the RFC 8292 `sub` contact a push service uses to reach the operator about abusive
    traffic; it must be a `mailto:` or `https:` URL.

    Unset → push is disabled: the subscribe endpoints return 503 and nothing is ever sent.
    """

    private_key_pem: SecretStr
    subject: str

    @field_validator("subject")
    @classmethod
    def _subject_must_be_contactable(cls, value: str) -> str:
        if not value.startswith(("mailto:", "https://")):
            raise ValueError("web_push.subject must be a mailto: or https:// contact URL")
        return value


class HostexecHostConfig(BaseModel):
    """One in-scope host for the `hostexec` in-process server.

    `daemon_id` selects the outbound node-daemon connection that receives work for this host.
    `audience_client_id` is the Authentik client_id of
    the host's `hostexec-<host>` provider — the audience the operator's token is exchanged for.
    """

    daemon_id: str
    audience_client_id: str


class HostexecConfig(BaseModel):
    """The `hostexec` in-process server: the in-scope hosts and the token-exchange scope.

    The Authentik token endpoint is derived from the operator OIDC issuer at composition (the same
    Authentik that authenticated the operator mints the per-host token from their identity), so it
    is not configured here. Lives in the console config file (`ConsoleConfigFile.hostexec`); unset
    there → the server is not offered.
    """

    hosts: dict[str, HostexecHostConfig]
    # Scopes requested on the per-host exchange. `groups` is required — the per-host provider's
    # `groups` scope mapping emits the operator's `hostexec-*` groups, which hostexecd checks for
    # `hostexec-<run_as>-<host>`; `openid` carries `sub` for the audit log. Configurable in case the
    # Authentik scope mapping is named differently.
    exchange_scope: str = "openid groups"


class NodeDaemonDefinition(BaseModel):
    """One outbound node daemon and the secret slot used to authenticate it."""

    display_name: str
    token_env_var: str
    backends: list[str] = Field(min_length=1)


class NodeDaemonsConfig(BaseModel):
    """Reusable heartbeat, long-poll, and lease policy for node execution daemons."""

    daemons: dict[str, NodeDaemonDefinition]
    heartbeat_interval_seconds: int = Field(default=10, ge=2, le=60)
    connected_after_seconds: int = Field(default=30, ge=5, le=300)
    offline_after_seconds: int = Field(default=60, ge=10, le=600)
    claim_wait_seconds: int = Field(default=20, ge=1, le=25)
    dispatch_timeout_seconds: int = Field(default=10, ge=1, le=60)
    lease_seconds: int = Field(default=30, ge=10, le=300)

    @model_validator(mode="after")
    def _ordered_presence_thresholds(self) -> Self:
        if self.offline_after_seconds <= self.connected_after_seconds:
            raise ValueError("offline_after_seconds must exceed connected_after_seconds")
        return self


class Settings(BaseSettings):
    # env_nested_delimiter so launch_routine.{routine_id,token} read from
    # HAKU_CONSOLE_LAUNCH_ROUTINE__{ROUTINE_ID,TOKEN}.
    model_config = SettingsConfigDict(env_prefix="HAKU_CONSOLE_", env_nested_delimiter="__")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load non-secret deployment settings from shared Console YAML below env overrides."""
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get("HAKU_CONSOLE_CONFIG_FILE"):
            sources.append(
                YamlConfigSettingsSource(settings_cls, yaml_file=config_file, yaml_config_section="settings")
            )
        sources.append(file_secret_settings)
        return tuple(sources)

    # Optional directory holding the built React SPA (index.html + assets), served
    # same-origin by FastAPI for direct local/dev fallback. Production leaves this
    # unset and serves the SPA from the haku-console-static nginx image.
    static_dir: Path | None = None

    # Projected ConfigMap file naming the independently deployed static image tag.
    # It is intentionally a file rather than an env var: Flux can update the static
    # Deployment and this metadata without rolling the API Deployment.
    static_image_tag_file: Path | None = None

    # Capability tier. launch_routine enables POST /api/capabilities/launch-routine
    # (None → the capability returns 503).
    launch_routine: LaunchRoutineConfig | None = None

    matrix: MatrixConfig | None = Field(
        default=None, description="Matrix chat surface. None → the sync loop does not run."
    )

    # The Authentik-gated origin of Haku's own UI service (runs in haku-sandbox), which the
    # console frames full-page as a sandboxed cross-origin iframe; the CSP allows framing it
    # plus Authentik's origin for the in-iframe SSO redirect. Required — framing haku-ui is
    # the console's whole job; set it to `about:blank` if there is genuinely no UI to frame.
    # The console never renders Haku's UI itself. See docs/containment.md.
    haku_ui_url: str
    auth_origin: str
    # Canonical public console origin used for OAuth redirects, secure-cookie policy, and
    # WebSocket origin checks. Required: there is no unauthenticated runtime mode.
    public_base_url: str

    # YAML file for required deploy-time console configuration that does not belong
    # in env vars. Secret values stay in env/Kubernetes Secret references; this file
    # names connected MCP servers, their env-backed credential slots, composable auto-approval
    # policies, static machine `agents` (id + env-referenced bearer + operator subject + policy),
    # Claude runtime wiring, and the `hostexec` host map (in-scope machines + exec URLs/audiences).
    config_file: Path

    # Non-secret runner topology selected by Console for every launched Agent. The runner turns
    # this into an ephemeral tokenFile kubeconfig backed by the exact-session bearer.
    runner_kubernetes_proxy_url: str | None = None
    # Haku's Agent-owned workspace bootstrap inside the shared runner image.
    haku_agent_workspace_setup: Path | None = None

    # Shared haku-console Postgres database. Required: it holds the MCP approval audit/result
    # ledger and the operator OAuth token store — the console does not run without them. Both
    # stores are always constructed; migrations are applied once at startup (see app.main).
    database_url: SecretStr

    # VAPID identity for Web Push. Reads HAKU_CONSOLE_WEB_PUSH__{PRIVATE_KEY_PEM,SUBJECT}.
    # Unset → the console never sends push notifications and the subscribe endpoints return 503.
    web_push: WebPushConfig | None = None

    # Outbound token endpoint budget for remote MCP operator OAuth. Refresh endpoints may
    # legitimately queue behind an authorization server's control-plane work; keep this larger
    # than httpx's historical 10-second default while retaining a bounded deployment knob.
    mcp_operator_oauth_token_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)

    # Maximum synchronous wait an Agent may request for an approval-gated MCP call. Required
    # deployment wiring: it must leave margin below the deployment's own request timeout.
    max_wait_for_result_ms: int = Field(ge=5_000)

    # The background reconciler refreshes every Operator's configured MCP catalogs this often.
    # `tools/list` itself reads only the already-published in-memory generation, so an upstream
    # connect, OAuth refresh, or large tool schema can never extend the client startup path.
    # This is also the dispatcher's successful-reflection reuse window and therefore the maximum
    # routine staleness budget for an upstream adding or removing a tool.
    mcp_catalog_refresh_interval_seconds: float = Field(default=60.0, ge=5.0, le=900.0)

    # Required when the config file lists the `haku_index` server, and unused otherwise: the
    # console refuses to start with search configured and nowhere to embed a query.
    embedder: EmbedderConfig | None = None
    # One configuration feeds every index reader and writer; HAKU_CONSOLE_RECALL_INDEX__CHUNK_BUDGET__*.
    recall_index: RecallIndexSettings = Field(default_factory=RecallIndexSettings)

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
    # Optional standing Kubernetes authorization policy. Absent means the
    # internal Kubernetes authorization endpoint remains fail-closed.

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
