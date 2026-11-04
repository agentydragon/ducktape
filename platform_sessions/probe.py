"""Shared checks and logging for installed provider sessions."""

from __future__ import annotations

import datetime
import json
import logging
import time

import httpx2

logger = logging.getLogger("uvicorn.error")


def probe_session(
    store, *, transport=None, source="periodic", checker, provider: str, instance_id: str | None = None
) -> dict:
    """Check the installed session and log the outcome without logging credentials."""
    identity = {"provider": provider}
    if instance_id is not None:
        identity["instance"] = instance_id
    try:
        session, imported_ns = store.snapshot()
    except (OSError, ValueError) as exc:
        event = {"event": "session_probe", **identity, "outcome": "storage_error", "source": source}
        logger.error("%s", json.dumps(event), exc_info=isinstance(exc, OSError))  # noqa: TRY400 - validation errors may include session data
        return event
    if session is None or imported_ns is None:
        event = {"event": "session_probe", **identity, "outcome": "uninitialized", "source": source}
        logger.info("%s", json.dumps(event))
        return event
    status = None
    failure: Exception | None = None
    try:
        checker(session, transport=transport)
        outcome = "success"
    except httpx2.HTTPStatusError as exc:
        failure = exc
        status = exc.response.status_code
        outcome = "reauth_required" if status == 401 else "upstream_http_error"
    except httpx2.HTTPError as exc:
        failure = exc
        outcome = "network_error"
    except ValueError as exc:
        failure = exc
        outcome = "invalid_response"
    # A concurrent import means this result no longer describes the current session.
    try:
        if store.snapshot() != (session, imported_ns):
            return {"event": "session_probe", **identity, "outcome": "superseded", "source": source}
    except (OSError, ValueError) as exc:
        event = {"event": "session_probe", **identity, "outcome": "storage_error", "source": source}
        logger.error("%s", json.dumps(event), exc_info=isinstance(exc, OSError))  # noqa: TRY400 - validation errors may include session data
        return event
    checked_at = datetime.datetime.now(datetime.UTC)
    imported_at = datetime.datetime.fromtimestamp(imported_ns / 1e9, datetime.UTC)
    event = {
        "event": "session_probe",
        **identity,
        "outcome": outcome,
        "source": source,
        "checked_at": checked_at.isoformat(),
        "imported_at": imported_at.isoformat(),
        "age_seconds": max(0, int(time.time() - imported_ns / 1e9)),
    }
    if status is not None:
        event["upstream_status"] = status
    if failure is None:
        logger.info("%s", json.dumps(event))
    else:
        logger.error("%s", json.dumps(event), exc_info=failure)
    return event
