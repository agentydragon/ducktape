"""Deploy configuration and wire vocabularies pinned to the Codex app-server client."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from haku.console.chat_models import RuntimeKind
from haku.console.http_url import uncredentialed_http_url

# Claim-owned exact-session authority. Keep these names local to the deploy-config layer rather
# than making schema/export binaries depend on the separately packaged runner implementation.
_RESERVED_SESSION_CREDENTIAL_ENV_VARS = frozenset({"HAKU_AGENT_SDK_RUNNER_TOKEN", "HAKU_MCP_BEARER_TOKEN"})


class CodexReasoningEffort(StrEnum):
    """The named reasoning-effort wire values of the pinned Codex client.

    A pinned third-party vocabulary (`ReasoningEffort` in
    `codex-rs/protocol/src/openai_models.rs` at tag `rust-v0.144.1`), so an unknown value is
    a config error, not a roll-tolerance case. Re-read that enum when the Codex image pin
    moves.
    """

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class CodexApprovalPolicy(StrEnum):
    """The plain-string approval policies of the pinned Codex app-server.

    A pinned third-party vocabulary (`AskForApproval` in
    `codex-rs/app-server-protocol/src/protocol/v2/shared.rs` at tag `rust-v0.144.1`). Upstream's
    experimental `granular` policy is an object rather than a string and is deliberately not
    representable here. Re-read that enum when the Codex image pin moves.
    """

    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"


class CodexSandboxMode(StrEnum):
    """The sandbox containment postures of the pinned Codex app-server.

    A pinned third-party vocabulary (`SandboxMode` in
    `codex-rs/app-server-protocol/src/protocol/v2/shared.rs` at tag `rust-v0.144.1`). Re-read
    that enum when the Codex image pin moves.
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class CodexAppServerImplementationConfig(BaseModel):
    """The settings that belong specifically to the Codex app-server implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[RuntimeKind.CODEX_APP_SERVER] = RuntimeKind.CODEX_APP_SERVER
    model: str
    # A field default rather than a required cluster config.yaml key: the ConfigMap applies
    # ahead of the image, and an image without this field would reject the unknown key at
    # startup via extra="forbid" (the 2026-07-14 config/image-skew outage class, haku/TODO.md).
    reasoning_effort: CodexReasoningEffort = CodexReasoningEffort.LOW
    provider_id: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    api_base_url: str
    api_key_env_var: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    github_token_placeholder: str

    @field_validator("api_key_env_var")
    @classmethod
    def _provider_key_must_not_alias_session_authority(cls, value: str) -> str:
        if value in _RESERVED_SESSION_CREDENTIAL_ENV_VARS:
            raise ValueError("provider key variable must not alias an exact-session credential")
        return value

    @field_validator("api_base_url")
    @classmethod
    def _uncredentialed_api_base_url(cls, value: str) -> str:
        return uncredentialed_http_url(value, field_name="api_base_url")
