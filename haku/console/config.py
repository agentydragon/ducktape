"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from mcp_infra.persistence import FilePersistence, PersistenceConfig

# Both URLs are built from the routine (trigger) id, so only the id + token are
# configured. The fire endpoint performs the launch; the claude.ai page is the
# operator-facing deep-link to review past runs (there's no runs-listing API).
_FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{id}/fire"
_PAGE_URL = "https://claude.ai/code/routines/{id}"


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

    When set, the console authenticates the operator's browser itself (Authentik authorization-code
    flow → signed session cookie), replacing the Authentik proxy outpost that guards
    `haku.allegedly.works` today. Agent access to `/mcp` uses its own MultiAuth and is unaffected.
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
    # Public console origin used for OAuth redirect URIs. When unset, the MCP
    # operator-auth API derives it from Host/X-Forwarded-* request headers.
    public_base_url: str | None = None

    # Optional YAML file for deploy-time console configuration that does not belong
    # in env vars. Secret values stay in env/Kubernetes Secret references; this file
    # names the connected MCP servers, the env-backed credential slot each uses, and the
    # static machine `agents` (each an agent id + env-referenced bearer + operator subject).
    config_file: Path | None = None

    # Shared haku-console Postgres database. Required: it holds the MCP approval audit/result
    # ledger and the operator OAuth token store — the console does not run without them. Both
    # stores are always constructed; migrations are applied once at startup (see app.main).
    database_url: SecretStr

    # Directory holding the Airlock-managed `haku_console_google` access token (files:
    # access_token, expires_at), mounted from the haku-console-google-access-token
    # secret. Backs the two in-process MCP servers built from this one grant: `gmail`
    # (haku.console.tools.gmail — search/read threads+messages+labels, draft creation,
    # thread-label changes, label CRUD) and `google_calendar` (haku.console.tools.google_calendar —
    # calendar event creation). Unset disables both servers (their capability entries
    # report `degraded`) and the Gmail thread-preview endpoint.
    google_token_dir: Path | None = None
    # Namespace whose Gmail label mutations Haku may auto-approve. labels_list is
    # wholesale because Haku already has standing Gmail read authority.
    gmail_auto_approve_label_prefix: str = "haku/"

    # Operator-facing console origin (e.g. https://haku.allegedly.works) used to build deep links
    # to a specific tool call in the SPA, returned in the MCP server's promise/read-tool results.
    # Unset → no link is included.
    ui_base_url: str | None = None

    # OAuth for the agent-facing MCP server: an Authentik-backed OIDCProxy handling the MCP OAuth
    # dance (DCR + PKCE) for claude.ai / the `claude` CLI, composed with the static agent bearer via
    # MultiAuth. Reads HAKU_CONSOLE_MCP_OAUTH__{OIDC_ISSUER,OIDC_CLIENT_ID,OIDC_CLIENT_SECRET,PUBLIC_BASE_URL}.
    # Unset → the static bearer is the only accepted credential (no OAuth).
    mcp_oauth: AuthentikAuthConfig | None = None
    # Client-state store for OIDCProxy's dynamic client registrations. Valkey in deploy (survives
    # across replicas / restarts); the file default suits single-process/dev.
    mcp_oauth_persistence: PersistenceConfig = Field(default_factory=FilePersistence)

    # Operator browser login (Authentik OIDC), replacing the proxy outpost. When set, `/api/*` and
    # the SPA require an operator session; unset → no app-level browser auth (outpost still guards).
    operator_oidc: OperatorOidcConfig | None = None
