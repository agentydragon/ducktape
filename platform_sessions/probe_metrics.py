"""Bounded, process-local metrics for completed provider session probes."""

from __future__ import annotations

import datetime
import threading
from collections.abc import Iterable

from prometheus_client import CollectorRegistry
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from platform_sessions.probe_types import ProbeOutcome, ProbeReporter, ProbeSource
from platform_sessions.registry import IntegrationInstance


class _ScopedProbeReporter:
    """Reporter scoped to one configured provider and instance."""

    def __init__(self, metrics: ProbeMetrics, provider: str, instance: str):
        self._metrics = metrics
        self._provider = provider
        self._instance = instance

    def completed(
        self, outcome: ProbeOutcome, source: ProbeSource, *, completed_at: datetime.datetime | None = None
    ) -> None:
        if not isinstance(outcome, ProbeOutcome):
            raise TypeError("outcome must be a ProbeOutcome")
        if not isinstance(source, ProbeSource):
            raise TypeError("source must be a ProbeSource")
        if completed_at is not None and (completed_at.tzinfo is None or completed_at.utcoffset() is None):
            raise ValueError("completed_at must be timezone-aware")
        self._metrics._record(self._provider, self._instance, outcome, source, completed_at)


class ProbeMetrics:
    """One registry collector containing metrics for configured instances only.

    The collector has no scrape-time I/O. Its counters and result timestamps live
    in memory and reset when the hosting process restarts.
    """

    _PROBE_HELP = "Number of completed provider session probes by sanitized outcome and source."
    _SUCCESS_HELP = "Unix timestamp of the latest successful probe; zero means none since process start."
    _LATEST_HELP = "One-hot outcome of the latest non-superseded completed probe; process-local and reset on restart."

    def __init__(self, registry: CollectorRegistry, integrations: Iterable[IntegrationInstance]):
        identities = [(item.type, item.id) for item in integrations]
        self._configured = frozenset(identities)
        if len(self._configured) != len(identities):
            raise ValueError("Duplicate integration identity")
        self._lock = threading.Lock()
        self._sequence = 0
        self._counts: dict[tuple[str, str, ProbeOutcome, ProbeSource], int] = {}
        self._last_success: dict[tuple[str, str], tuple[float, int]] = {}
        self._latest: dict[tuple[str, str], tuple[float, int, ProbeOutcome]] = {}
        registry.register(self)

    def reporter_for(self, provider: str, instance: str) -> ProbeReporter:
        """Return the reporting interface for an exact configured identity."""
        if (provider, instance) not in self._configured:
            raise ValueError("Probe metrics identity is not configured")
        return _ScopedProbeReporter(self, provider, instance)

    def _record(
        self,
        provider: str,
        instance: str,
        outcome: ProbeOutcome,
        source: ProbeSource,
        completed_at: datetime.datetime | None,
    ) -> None:
        identity = (provider, instance)
        if identity not in self._configured:
            raise ValueError("Probe metrics identity is not configured")
        timestamp = (
            completed_at.timestamp() if completed_at is not None else datetime.datetime.now(datetime.UTC).timestamp()
        )
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            count_key = (provider, instance, outcome, source)
            self._counts[count_key] = self._counts.get(count_key, 0) + 1
            if outcome is ProbeOutcome.SUPERSEDED:
                return
            result_key = (timestamp, sequence)
            latest = self._latest.get(identity)
            if latest is None or result_key >= latest[:2]:
                self._latest[identity] = (timestamp, sequence, outcome)
            if outcome is ProbeOutcome.SUCCESS:
                last_success = self._last_success.get(identity)
                if last_success is None or result_key >= last_success:
                    self._last_success[identity] = result_key

    @staticmethod
    def _families():
        return (
            CounterMetricFamily(
                "platform_session_probe", ProbeMetrics._PROBE_HELP, labels=["provider", "instance", "outcome", "source"]
            ),
            GaugeMetricFamily(
                "platform_session_last_success_timestamp_seconds",
                ProbeMetrics._SUCCESS_HELP,
                labels=["provider", "instance"],
            ),
            GaugeMetricFamily(
                "platform_session_latest_probe_outcome",
                ProbeMetrics._LATEST_HELP,
                labels=["provider", "instance", "outcome"],
            ),
        )

    def describe(self):
        yield from self._families()

    def collect(self):
        with self._lock:
            counts = dict(self._counts)
            last_success = dict(self._last_success)
            latest = dict(self._latest)
        probes, success, outcomes = self._families()
        for (provider, instance, outcome, source), value in sorted(
            counts.items(), key=lambda item: tuple(str(part) for part in item[0])
        ):
            probes.add_metric([provider, instance, outcome.value, source.value], value)
        for provider, instance in sorted(self._configured):
            success.add_metric([provider, instance], last_success.get((provider, instance), (0.0, 0))[0])
            current = latest.get((provider, instance))
            for outcome in ProbeOutcome:
                outcomes.add_metric([provider, instance, outcome.value], 1 if current and current[2] is outcome else 0)
        yield probes
        yield success
        yield outcomes
