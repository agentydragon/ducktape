"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
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


class EmbedderConfig(BaseModel):
    """Where the `haku_index` tools compute embeddings: any OpenAI-compatible `/v1/embeddings`.

    Ollama today, LiteLLM or anything else that speaks the format tomorrow — which is why this is
    a URL and a model name rather than a backend choice.

    `model` is also the index's `model_key`, so it names the model and not the deployment: point
    it at a different server serving the same model and every cached vector is still valid; point
    it at a different model and the cache misses by construction rather than by anyone noticing.
    """

    base_url: str = Field(description="Base URL including the API version, e.g. http://haku-embedder:8080/v1")
    model: str
    # Instruction-aware models want queries prefixed and documents plain (Qwen3-Embedding, bge,
    # E5). It belongs to the model rather than to the endpoint, so it is configured beside the
    # model name; a model without that asymmetry leaves it empty.
    query_instruction: str = ""
    # The client library requires one; Ollama ignores it, a hosted endpoint would not.
    api_key: SecretStr = SecretStr("not-used")
    # Explicit because the client library's default is ten minutes, and this sits on the search
    # request path: a slow embedder should fail a search, not hold a connection until the caller
    # gives up. Generous enough for a cold model load, short enough to be an error rather than a
    # hang — and it wants to be, since Ollama is a zone away from this pod.
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    # The sync sweeps embed batches of documents off the request path, where waiting out a cold
    # model load is what you want and giving up means the corpus simply never fills.
    sync_timeout_seconds: float = Field(default=300.0, gt=0.0)


class RecallIndexConfig(BaseModel):
    """Retrieval-unit sizing shared by the console's index writers and readers.

    The same complete budget must reach both paths: it is serialized into ``chunker_key``, so a
    reader under another budget would search a regime the writers never produced.
    """

    chunk_budget: ChunkBudget = Field(default=DEFAULT_CHUNK_BUDGET)


class HakuStateGitConfig(BaseModel):
    """The read side of haku-state, for the index's `git` corpus.

    Configured means the console syncs that corpus; unset means it serves whatever the `chat`
    corpus holds and nothing else. The credential is Haku's own Forgejo account (operator,
    2026-08-15), reflected into this namespace — so the console now holds a credential that can
    also *write* haku-state, which `haku/console/README.md` records. Nothing here writes: the
    mirror is fetched, never pushed to.
    """

    repo_url: str
    branch: str = "main"
    username: str | None = None
    password: SecretStr | None = None
    # A bare mirror, on ephemeral pod storage by default: losing it costs a clone, not an
    # embedding, since the chunk cache is content-addressed and lives in Postgres.
    mirror_path: Path = Path("/tmp/haku-recall-index/mirror.git")


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
    """Wiring for the Matrix chat surface (<x/channels/matrix/SPEC.md>).

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


class ClaudeRuntimeConfig(BaseModel):
    """Explicit deploy-time wiring for the Console-owned Claude chat runtime."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    warm_pool: str
    cwd: str
    session_ttl_seconds: int = Field(ge=300, le=86400)
    oauth_placeholder: str
    https_proxy: str
    ca_bundle: str
    no_proxy: str
    mcp_url: str
    mcp_static_agent_id: UUID
    # Absolute, like every other path here: mounted beside this config file in the console's
    # ConfigMap. Rendered by `haku.console.x.system_prompt`, which says why it is deploy
    # config rather than code or haku-state.
    system_prompt_template: Path

    def claude_environment(self) -> dict[str, str]:
        return {
            "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_placeholder,
            "HTTP_PROXY": self.https_proxy,
            "HTTPS_PROXY": self.https_proxy,
            "NO_PROXY": self.no_proxy,
            "NODE_USE_ENV_PROXY": "1",
            "NODE_EXTRA_CA_CERTS": self.ca_bundle,
            "SSL_CERT_FILE": self.ca_bundle,
            "CURL_CA_BUNDLE": self.ca_bundle,
            "REQUESTS_CA_BUNDLE": self.ca_bundle,
        }


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

    # Optional directory holding the built React SPA (index.html + assets), served
    # same-origin by FastAPI for direct local/dev fallback. Production leaves this
    # unset and serves the SPA from the haku-console-static nginx image.
    static_dir: Path | None = None

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
    auth_origin: str = "https://auth.allegedly.works"
    # Canonical public console origin used for OAuth redirects, secure-cookie policy, and
    # WebSocket origin checks. Required: there is no unauthenticated runtime mode.
    public_base_url: str

    # Optional YAML file for deploy-time console configuration that does not belong
    # in env vars. Secret values stay in env/Kubernetes Secret references; this file
    # names connected MCP servers, their env-backed credential slots, composable auto-approval
    # policies, static machine `agents` (id + env-referenced bearer + operator subject + policy),
    # Claude runtime wiring, and the `hostexec` host map (in-scope machines + exec URLs/audiences).
    config_file: Path | None = None

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

    # How long one upstream server's reflected tool catalog stays reusable. `tools/list` reflects
    # every configured server live and `stateless_http=True` means that happens on every request,
    # so without this the aggregate listing pays a full MCP connect per server per request.
    # Bounded low because nothing invalidates on an upstream adding a tool: this is a staleness
    # budget, not a cache lifetime. 0 disables reuse across requests but still collapses concurrent
    # reflections of the same server.
    mcp_catalog_cache_ttl_seconds: float = Field(default=60.0, ge=0.0, le=900.0)

    # Required when the config file lists the `haku_index` server, and unused otherwise: the
    # console refuses to start with search configured and nowhere to embed a query.
    embedder: EmbedderConfig | None = None
    # One configuration feeds every index reader and writer; HAKU_CONSOLE_RECALL_INDEX__CHUNK_BUDGET__*.
    recall_index: RecallIndexConfig = Field(default_factory=RecallIndexConfig)
    # Where the index's `git` corpus comes from. Unset leaves that corpus empty and only the
    # `chat` corpus — which the console builds from its own tables — searchable.
    haku_state_git: HakuStateGitConfig | None = None

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
