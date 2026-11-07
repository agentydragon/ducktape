"""Typed, bounded provider probe outcomes and reporting interface."""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Protocol


class ProbeOutcome(StrEnum):
    """Finite set of sanitized probe outcomes accepted by the metrics API."""

    SUCCESS = "success"
    UNINITIALIZED = "uninitialized"
    STORAGE_ERROR = "storage_error"
    CHALLENGE = "challenge"
    REAUTH_REQUIRED = "reauth_required"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UPSTREAM_HTTP_ERROR = "upstream_http_error"
    NETWORK_ERROR = "network_error"
    INVALID_RESPONSE = "invalid_response"
    SUPERSEDED = "superseded"
    INTERNAL_ERROR = "internal_error"


class ProbeSource(StrEnum):
    """Finite source labels for a completed probe."""

    PERIODIC = "periodic"
    VERIFY = "verify"
    STARTUP = "startup"
    MANUAL = "manual"
    OTHER = "other"


class ProbeReporter(Protocol):
    """Provider-facing reporting interface scoped to one configured instance."""

    def completed(
        self, outcome: ProbeOutcome, source: ProbeSource, *, completed_at: datetime.datetime | None = None
    ) -> None:
        """Record a completed probe using only fixed outcome and source values."""
        ...
