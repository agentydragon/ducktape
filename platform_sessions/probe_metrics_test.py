"""Provider-scoped probe metrics stay bounded and preserve completion ordering."""

import datetime

import pytest
import pytest_bazel
from prometheus_client import CollectorRegistry, generate_latest
from pydantic import BaseModel

from platform_sessions.probe_metrics import ProbeMetrics
from platform_sessions.probe_types import ProbeOutcome, ProbeSource
from platform_sessions.registry import IntegrationRegistry, IntegrationType


class Settings(BaseModel):
    pass


def configured_instances():
    return IntegrationRegistry((IntegrationType("example", Settings, "https://example.test"),)).resolve(
        {"integrations": [{"id": "first", "type": "example"}, {"id": "second", "type": "example"}]}
    )


def scrape(registry: CollectorRegistry) -> str:
    return generate_latest(registry).decode("utf-8")


def test_metrics_are_instance_scoped_and_only_accept_fixed_labels():
    registry = CollectorRegistry()
    first, second = configured_instances()
    metrics = ProbeMetrics(registry, [first, second])
    reporter = metrics.reporter_for(first.type, first.id)

    reporter.completed(ProbeOutcome.SUCCESS, ProbeSource.PERIODIC)
    reporter.completed(ProbeOutcome.CHALLENGE, ProbeSource.VERIFY)

    assert (
        registry.get_sample_value(
            "platform_session_probe_total",
            {"provider": "example", "instance": "first", "outcome": "success", "source": "periodic"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "platform_session_probe_total",
            {"provider": "example", "instance": "first", "outcome": "challenge", "source": "verify"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "platform_session_probe_total",
            {"provider": "example", "instance": "second", "outcome": "success", "source": "periodic"},
        )
        is None
    )
    assert (
        registry.get_sample_value(
            "platform_session_latest_probe_outcome",
            {"provider": "example", "instance": "first", "outcome": "challenge"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "platform_session_latest_probe_outcome", {"provider": "example", "instance": "second", "outcome": "success"}
        )
        == 0
    )

    with pytest.raises(ValueError, match="not configured"):
        metrics.reporter_for("example", "unconfigured")
    with pytest.raises(TypeError, match="ProbeOutcome"):
        reporter.completed("provider supplied secret", ProbeSource.VERIFY)  # type: ignore[arg-type]
    assert "provider supplied secret" not in scrape(registry)


def test_latest_outcome_and_success_timestamp_ignore_stale_and_superseded_results():
    registry = CollectorRegistry()
    config = configured_instances()[0]
    metrics = ProbeMetrics(registry, [config])
    reporter = metrics.reporter_for(config.type, config.id)

    def timestamp(seconds):
        return datetime.datetime.fromtimestamp(seconds, datetime.UTC)

    reporter.completed(ProbeOutcome.SUCCESS, ProbeSource.PERIODIC, completed_at=timestamp(100))
    reporter.completed(ProbeOutcome.OUTCOME_UNKNOWN, ProbeSource.VERIFY, completed_at=timestamp(200))
    reporter.completed(ProbeOutcome.SUCCESS, ProbeSource.PERIODIC, completed_at=timestamp(150))
    reporter.completed(ProbeOutcome.SUPERSEDED, ProbeSource.PERIODIC, completed_at=timestamp(300))

    identity = {"provider": "example", "instance": "first"}
    assert registry.get_sample_value("platform_session_last_success_timestamp_seconds", identity) == 150
    assert (
        registry.get_sample_value("platform_session_latest_probe_outcome", identity | {"outcome": "outcome_unknown"})
        == 1
    )
    assert registry.get_sample_value("platform_session_latest_probe_outcome", identity | {"outcome": "superseded"}) == 0
    assert (
        registry.get_sample_value(
            "platform_session_probe_total", identity | {"outcome": "superseded", "source": "periodic"}
        )
        == 1
    )

    reporter.completed(ProbeOutcome.SUCCESS, ProbeSource.VERIFY, completed_at=timestamp(400))
    assert registry.get_sample_value("platform_session_last_success_timestamp_seconds", identity) == 400
    assert registry.get_sample_value("platform_session_latest_probe_outcome", identity | {"outcome": "success"}) == 1

    with pytest.raises(ValueError, match="timezone-aware"):
        reporter.completed(ProbeOutcome.SUCCESS, ProbeSource.VERIFY, completed_at=datetime.datetime(2026, 1, 1))


if __name__ == "__main__":
    pytest_bazel.main()
