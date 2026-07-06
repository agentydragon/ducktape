"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Operator-approved MCP tool-call substrate. The catalog path points at the
    # connected MCP server registry mounted/configured into the console; each server's
    # tool schema is reflected from the MCP server itself. Production stores the
    # approval ledger in Postgres. If the database URL and store path are unset,
    # tests/local dev use in-memory state; store_path is only a local fallback.
    mcp_approval_catalog_path: Path | None = None
    mcp_approval_database_url: SecretStr | None = None
    mcp_approval_store_path: Path | None = None
    # Optional bearer accepted from Haku / haku-ui backend for backend-to-backend calls.
    # Browser/operator calls still rely on the Authentik session at the ingress.
    mcp_approval_api_token: SecretStr | None = None
