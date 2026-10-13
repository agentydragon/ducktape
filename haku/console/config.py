"""Typed process configuration shared by Haku Console settings models."""

from __future__ import annotations

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# The one environment variable read before the YAML source exists; every other setting is
# `HAKU_CONSOLE__*` (`settings.Settings`).
CONFIG_FILE_ENV = "HAKU_CONSOLE_CONFIG_FILE"

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

    The console authenticates the operator's browser itself (Authentik authorization-code flow →
    signed session cookie). The static Agent credentials are used by the internal Kubernetes
    authorization proxy, independently of browser login.
    Reads `HAKU_CONSOLE__OPERATOR_OIDC__{ISSUER,CLIENT_ID,CLIENT_SECRET,SESSION_SECRET}`. The redirect
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


class ConsoleProcessConfig(BaseModel):
    """Process-local fields combined with the deploy catalog by ``settings.Settings``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Optional directory holding the built React SPA (index.html + assets), served
    # same-origin by FastAPI for direct local/dev fallback. Production leaves this
    # unset and serves the SPA from the haku-console-static nginx image.
    static_dir: Path | None = None

    # Projected ConfigMap file naming the independently deployed static image tag.
    # It is intentionally a file rather than an env var: Flux can update the static
    # Deployment and this metadata without rolling the API Deployment.
    static_image_tag_file: Path | None = None
    image_tag: str | None = None
    static_image_tag: str | None = None

    # Capability tier. launch_routine enables POST /api/capabilities/launch-routine
    # (None → the capability returns 503).
    launch_routine: LaunchRoutineConfig | None = None

    # The Authentik-gated origin of Haku's own UI service (runs in haku-sandbox), which the
    # console frames full-page as a sandboxed cross-origin iframe; the CSP allows framing it
    # plus Authentik's origin for the in-iframe SSO redirect. Required — framing haku-ui is
    # the console's whole job; set it to `about:blank` if there is genuinely no UI to frame.
    # The console never renders Haku's UI itself. See docs/containment.md.
    haku_ui_url: str
    auth_origin: str
    # Canonical public console origin used for OAuth redirects, secure-cookie policy, and
    # WebSocket origin checks. Required: there is no unauthenticated harness mode.
    public_base_url: str

    # Bootstrap path for the YAML settings source. It deliberately retains its established
    # single-underscore environment name while ordinary settings use HAKU_CONSOLE__*.
    config_file: Path = Field(validation_alias=AliasChoices("config_file", CONFIG_FILE_ENV))

    # Shared haku-console Postgres database. Required: it holds the MCP approval audit/result
    # ledger and the operator OAuth token stores — the console does not run without them. Both
    # stores are always constructed; migrations are applied once at startup (see app.main).
    database_url: SecretStr

    # Maximum synchronous wait an Agent may request for an approval-gated MCP call. Required
    # deployment wiring: it must leave margin below the deployment's own request timeout.
    max_wait_for_result_ms: int = Field(ge=5_000)

    # Operator browser login (Authentik OIDC), replacing the proxy outpost. Required in every
    # harness, including development; tests use the repo's hermetic OIDC fixture.
    operator_oidc: OperatorOidcConfig
    # Canonical Operator identity trust contract. Required in every harness; see
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
