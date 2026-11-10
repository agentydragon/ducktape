"""Shared checks and logging for installed provider sessions."""

from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass

import httpx2

from platform_sessions.probe_types import ProbeOutcome, ProbeSource

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class ProbeResult:
    """Typed completion of one installed-session probe."""

    provider: str
    instance_id: str | None
    outcome: ProbeOutcome
    source: ProbeSource
    checked_at: datetime.datetime | None = None
    imported_at: datetime.datetime | None = None
    age_seconds: int | None = None
    upstream_status: int | None = None

    def as_event(self) -> dict[str, str | int]:
        """Return the existing sanitized JSON event shape for status and logs."""
        event: dict[str, str | int] = {"event": "session_probe", "provider": self.provider}
        if self.instance_id is not None:
            event["instance"] = self.instance_id
        event["outcome"] = self.outcome.value
        event["source"] = self.source.value
        if self.checked_at is not None:
            event["checked_at"] = self.checked_at.isoformat()
        if self.imported_at is not None:
            event["imported_at"] = self.imported_at.isoformat()
        if self.age_seconds is not None:
            event["age_seconds"] = self.age_seconds
        if self.upstream_status is not None:
            event["upstream_status"] = self.upstream_status
        return event


def probe_session(
    store,
    *,
    transport=None,
    source: ProbeSource = ProbeSource.PERIODIC,
    checker,
    provider: str,
    instance_id: str | None = None,
) -> ProbeResult:
    """Check the installed session and log the outcome without logging credentials."""
    try:
        session, imported_ns = store.snapshot()
    except (OSError, ValueError) as exc:
        result = ProbeResult(provider, instance_id, ProbeOutcome.STORAGE_ERROR, source)
        logger.error("%s", json.dumps(result.as_event()), exc_info=isinstance(exc, OSError))  # noqa: TRY400 - validation errors may include session data
        return result
    if session is None or imported_ns is None:
        result = ProbeResult(provider, instance_id, ProbeOutcome.UNINITIALIZED, source)
        logger.info("%s", json.dumps(result.as_event()))
        return result
    status: int | None = None
    failure: Exception | None = None
    try:
        checker(session, transport=transport)
        outcome = ProbeOutcome.SUCCESS
    except httpx2.HTTPStatusError as exc:
        failure = exc
        status = exc.response.status_code
        outcome = ProbeOutcome.REAUTH_REQUIRED if status == 401 else ProbeOutcome.UPSTREAM_HTTP_ERROR
    except httpx2.HTTPError as exc:
        failure = exc
        outcome = ProbeOutcome.NETWORK_ERROR
    except ValueError as exc:
        failure = exc
        outcome = ProbeOutcome.INVALID_RESPONSE
    # A concurrent import means this result no longer describes the current session.
    try:
        if store.snapshot() != (session, imported_ns):
            return ProbeResult(provider, instance_id, ProbeOutcome.SUPERSEDED, source)
    except (OSError, ValueError) as exc:
        result = ProbeResult(provider, instance_id, ProbeOutcome.STORAGE_ERROR, source)
        logger.error("%s", json.dumps(result.as_event()), exc_info=isinstance(exc, OSError))  # noqa: TRY400 - validation errors may include session data
        return result
    checked_at = datetime.datetime.now(datetime.UTC)
    imported_at = datetime.datetime.fromtimestamp(imported_ns / 1e9, datetime.UTC)
    result = ProbeResult(
        provider,
        instance_id,
        outcome,
        source,
        checked_at,
        imported_at,
        max(0, int(time.time() - imported_ns / 1e9)),
        status,
    )
    event = result.as_event()
    if failure is None:
        logger.info("%s", json.dumps(event))
    else:
        logger.error("%s", json.dumps(event), exc_info=failure)
    return result
