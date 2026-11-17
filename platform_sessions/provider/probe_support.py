"""Provider control helpers for recording and encoding probe completions."""

from __future__ import annotations

import datetime
import threading
from collections.abc import Callable

from platform_sessions.probe_types import ProbeOutcome, ProbeReporter, ProbeSource
from platform_sessions.provider import session_provider_pb2 as pb

type ProbeRecord = tuple[ProbeOutcome, ProbeSource, datetime.datetime]


class ProbeRecorder:
    """Record completed probes and forward them to the instance's reporter."""

    def __init__(
        self, reporter: ProbeReporter, on_completed: Callable[[ProbeOutcome, datetime.datetime], None] | None = None
    ) -> None:
        self.reporter = reporter
        self.on_completed = on_completed
        self._last: ProbeRecord | None = None
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def last(self) -> ProbeRecord | None:
        with self._lock:
            return self._last

    @last.setter
    def last(self, value: ProbeRecord | None) -> None:
        with self._lock:
            self._last = value

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def completed(
        self, outcome: ProbeOutcome, source: ProbeSource, *, completed_at: datetime.datetime | None = None
    ) -> None:
        completed_at = completed_at or datetime.datetime.now(datetime.UTC)
        with self._lock:
            self._last = (outcome, source, completed_at)
            self._sequence += 1
            if self.on_completed is not None:
                self.on_completed(outcome, completed_at)
            self.reporter.completed(outcome, source, completed_at=completed_at)


def probe_source_to_proto(source: ProbeSource) -> pb.ProbeSource:
    """Map every completed-probe source to its public result enum."""
    sources = {
        ProbeSource.PERIODIC: pb.PROBE_SOURCE_SCHEDULED,
        ProbeSource.MANUAL: pb.PROBE_SOURCE_MANUAL,
        ProbeSource.VERIFY: pb.PROBE_SOURCE_VERIFY,
        ProbeSource.STARTUP: pb.PROBE_SOURCE_STARTUP,
        ProbeSource.OTHER: pb.PROBE_SOURCE_OTHER,
    }
    try:
        return sources[source]
    except KeyError as error:
        raise ValueError(f"Unsupported probe source: {source}") from error


def probe_request_source_from_proto(source: int) -> ProbeSource:
    """Decode the ProbeRequest source contract (scheduled or manual)."""
    sources: dict[int, ProbeSource] = {
        pb.PROBE_SOURCE_SCHEDULED: ProbeSource.PERIODIC,
        pb.PROBE_SOURCE_MANUAL: ProbeSource.MANUAL,
    }
    try:
        return sources[source]
    except KeyError as error:
        raise ValueError("ProbeRequest source must be scheduled or manual") from error


def probe_result(outcome: ProbeOutcome, source: ProbeSource, completed_at: datetime.datetime) -> pb.ProbeResult:
    """Encode a classified probe; callers report ambiguous side effects as OUTCOME_UNKNOWN."""
    result = pb.ProbeResult(source=probe_source_to_proto(source))
    result.completed_at.FromDatetime(completed_at)
    if outcome is ProbeOutcome.SUCCESS:
        result.verified.SetInParent()
    elif outcome is ProbeOutcome.UNINITIALIZED:
        result.no_session.SetInParent()
    elif outcome is ProbeOutcome.REAUTH_REQUIRED:
        result.reauthentication_required.SetInParent()
    elif outcome is ProbeOutcome.CHALLENGE:
        result.challenge_required.SetInParent()
    elif outcome is ProbeOutcome.NETWORK_ERROR:
        result.upstream_unavailable.SetInParent()
    elif outcome is ProbeOutcome.OUTCOME_UNKNOWN:
        result.outcome_unknown.credential_rotation_may_have_occurred = True
    elif outcome in {
        ProbeOutcome.STORAGE_ERROR,
        ProbeOutcome.UPSTREAM_HTTP_ERROR,
        ProbeOutcome.INVALID_RESPONSE,
        ProbeOutcome.SUPERSEDED,
        ProbeOutcome.INTERNAL_ERROR,
    }:
        result.failure.reason = {
            ProbeOutcome.STORAGE_ERROR: pb.PROBE_FAILURE_REASON_STORAGE_ERROR,
            ProbeOutcome.UPSTREAM_HTTP_ERROR: pb.PROBE_FAILURE_REASON_UPSTREAM_HTTP_ERROR,
            ProbeOutcome.INVALID_RESPONSE: pb.PROBE_FAILURE_REASON_INVALID_RESPONSE,
            ProbeOutcome.SUPERSEDED: pb.PROBE_FAILURE_REASON_SUPERSEDED,
            ProbeOutcome.INTERNAL_ERROR: pb.PROBE_FAILURE_REASON_PROVIDER_INTERNAL,
        }[outcome]
    else:
        raise ValueError("Unsupported probe outcome")
    return result
