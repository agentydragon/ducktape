"""Provider-neutral session installation and probe result types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from platform_sessions.probe import ProbeResult


class SessionInstallKind(Enum):
    INSTALLED = "installed"
    REAUTH_REQUIRED = "reauth_required"
    CHALLENGE_REQUIRED = "challenge_required"
    CANDIDATE_REJECTED = "candidate_rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"


class SessionInstallFailure(Enum):
    INVALID_RESPONSE = "invalid_response"
    UPSTREAM_HTTP_ERROR = "upstream_http_error"
    VERIFICATION_TIMEOUT = "verification_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANDIDATE_INVALID = "candidate_invalid"


class OperationUnknownReason(Enum):
    TIMEOUT = "timeout"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True)
class SessionInstallResult:
    kind: SessionInstallKind
    failure: SessionInstallFailure | None = None
    error_code: str | None = None
    error_status: int | None = None
    upstream_status: int | None = None


@dataclass(frozen=True)
class ProviderProbeResult:
    probe: ProbeResult
    unknown_reason: OperationUnknownReason | None = None
    unknown_failure: SessionInstallFailure | None = None
    upstream_status: int | None = None
