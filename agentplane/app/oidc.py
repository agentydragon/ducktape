"""Static OIDC settings and the explicit runtime credential inputs."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECURE_COOKIE = "__Host-agentplane_session"
INSECURE_COOKIE = "agentplane_session"


class OIDCSettings(BaseModel):
    """Non-secret OIDC relying-party settings authored in the app YAML file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # URL normalization must not add or remove a slash from an OIDC issuer identifier.
    issuer: str = Field(min_length=1, description="The provider's exact HTTPS issuer identifier.")
    public_base_url: str = Field(min_length=1, description="The app's public origin used to build the redirect URI.")
    session_max_seconds: int = Field(
        default=604800, gt=0, description="How long a login lasts at most, however it is used."
    )
    session_idle_seconds: int = Field(
        default=86400, gt=0, description="How long a login lasts without an authenticated request."
    )
    session_activity_step_seconds: int = Field(
        default=300, gt=0, description="Activity moves the idle deadline only once it would move by more than this."
    )
    token_renew_before_seconds: int = Field(
        default=30, ge=0, description="Renew the login access token once it is this close to expiry."
    )
    static_ed25519_keys: frozenset[str] | None = Field(
        default=None,
        min_length=1,
        description="Unpadded base64url Ed25519 public keys verifying bearer ID tokens in place of the issuer's "
        "discovered keys. Only an issuer under the reserved .test domain may pin keys; omit to use discovery.",
    )

    @model_validator(mode="after")
    def _pin_only_test_issuers(self) -> OIDCSettings:
        if self.static_ed25519_keys is not None and not (urlsplit(self.issuer).hostname or "").endswith(".test"):
            raise ValueError("static_ed25519_keys requires an issuer under the reserved .test domain")
        return self


class OIDCSecretInputs(BaseModel):
    """Credential fields supplied by the runtime Secret inputs, never the settings file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    session_secret: str = Field(min_length=1)
