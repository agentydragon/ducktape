"""Builds HTTP and gRPC `Probe`s from just the fields that vary between callers."""

from __future__ import annotations

from cdk8s import Duration
from cdk8s_plus_34 import Probe, k8s


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


def k8s_http_probe(
    path: str,
    *,
    port: int,
    initial_delay_seconds: int,
    period_seconds: int = 10,
    timeout_seconds: int | None = None,
    failure_threshold: int | None = None,
) -> k8s.Probe:
    """Build an HTTP probe struct for a raw `k8s.Container` field.

    Use `http_probe` for cdk8s-plus workload builders, whose probe field accepts `Probe` objects.
    """
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path=path, port=k8s.IntOrString.from_number(port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
        timeout_seconds=timeout_seconds,
        failure_threshold=failure_threshold,
    )


def grpc_probe(*, port: int, initial_delay_seconds: int, period_seconds: int = 10, service: str | None = None) -> Probe:
    """Calls the standard `grpc.health.v1.Health/Check` for `service`, the whole server when unset."""
    return Probe.from_grpc(
        port=port,
        service=service,
        initial_delay_seconds=Duration.seconds(initial_delay_seconds),
        period_seconds=Duration.seconds(period_seconds),
    )
