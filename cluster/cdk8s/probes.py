"""Builds an HTTP `Probe` from just the fields that vary between callers."""

from __future__ import annotations

from cdk8s import Duration
from cdk8s_plus_34 import Probe


def http_probe(
    path: str,
    *,
    port: int,
    initial_delay_seconds: int,
    period_seconds: int = 10,
    timeout_seconds: int | None = None,
    failure_threshold: int | None = None,
) -> Probe:
    return Probe.from_http_get(
        path,
        port=port,
        initial_delay_seconds=Duration.seconds(initial_delay_seconds),
        period_seconds=Duration.seconds(period_seconds),
        timeout_seconds=Duration.seconds(timeout_seconds) if timeout_seconds is not None else None,
        failure_threshold=failure_threshold,
    )
