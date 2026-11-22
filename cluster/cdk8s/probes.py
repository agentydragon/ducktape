"""Builds HTTP and gRPC `Probe`s from just the fields that vary between callers."""

from __future__ import annotations

from dataclasses import dataclass

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


def grpc_probe(
    *, port: int, initial_delay_seconds: int, period_seconds: int = 10, failure_threshold: int | None = None
) -> Probe:
    """Calls the standard `grpc.health.v1.Health/Check` for the whole server."""
    return Probe.from_grpc(
        port=port,
        initial_delay_seconds=Duration.seconds(initial_delay_seconds),
        period_seconds=Duration.seconds(period_seconds),
        failure_threshold=failure_threshold,
    )


@dataclass(frozen=True, kw_only=True)
class TlsGrpcHealth:
    """A workload's local gRPC health endpoint and the identity its probes must verify."""

    port: int
    ca_file: str
    server_uri: str
    # Servers that distinguish process health from readiness register a separate service.
    liveness_service: str | None = None

    def readiness(self) -> Probe:
        return self._probe(initial_delay_seconds=3, period_seconds=10)

    def liveness(self, *, initial_delay_seconds: int = 20) -> Probe:
        return self._probe(
            service=self.liveness_service, initial_delay_seconds=initial_delay_seconds, period_seconds=30
        )

    def startup(self, *, failure_threshold: int = 60) -> Probe:
        return self._probe(
            service=self.liveness_service,
            initial_delay_seconds=0,
            period_seconds=5,
            failure_threshold=failure_threshold,
        )

    def _probe(
        self,
        *,
        initial_delay_seconds: int,
        period_seconds: int,
        service: str | None = None,
        failure_threshold: int | None = None,
    ) -> Probe:
        command = [
            "/healthprobe",
            "--target",
            f"127.0.0.1:{self.port}",
            "--ca-file",
            self.ca_file,
            "--server-uri",
            self.server_uri,
        ]
        if service is not None:
            command.extend(("--service", service))
        return Probe.from_command(
            command,
            initial_delay_seconds=Duration.seconds(initial_delay_seconds),
            period_seconds=Duration.seconds(period_seconds),
            failure_threshold=failure_threshold,
            timeout_seconds=Duration.seconds(5),
        )
