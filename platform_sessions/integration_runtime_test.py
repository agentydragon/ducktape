"""The common lifecycle delegates provider checks without persisting rejected sessions."""

import pytest
import pytest_bazel
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from pydantic import BaseModel, TypeAdapter

from platform_sessions.host import IntegrationError
from platform_sessions.integration_runtime import IntegrationRuntime
from platform_sessions.probe_metrics import ProbeMetrics
from platform_sessions.probe_types import ProbeSource
from platform_sessions.registry import IntegrationRegistry, IntegrationType
from platform_sessions.session_store import ProviderSessionStore


class Session(BaseModel):
    token: str


class Settings(BaseModel):
    pass


def sample_value(output: str, name: str, labels: dict[str, str]) -> float | None:
    return next(
        (
            sample.value
            for family in text_string_to_metric_families(output)
            for sample in family.samples
            if sample.name == name and sample.labels == labels
        ),
        None,
    )


class ExampleIntegration(IntegrationRuntime[Session]):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_refresh = False

    def parse(self, body: bytes) -> Session:
        return Session.model_validate_json(body)

    def verify(self, candidate: Session, *, transport=None) -> None:
        if candidate.token == "bad" or self.fail_refresh:
            raise ValueError("rejected")


@pytest.mark.asyncio
async def test_candidate_verification_and_install_preserve_previous_session(tmp_path):
    config = IntegrationRegistry((IntegrationType("example", Settings, "https://example.test"),)).resolve(
        {"integrations": [{"id": "test", "type": "example", "state_dir": str(tmp_path)}]}
    )[0]
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session))
    runtime = ExampleIntegration(config, store)
    assert (await runtime.status())["state"] == "uninitialized"
    good = runtime.parse(b'{"token":"good"}')
    await runtime.install(good)
    original = store.snapshot()
    assert (await runtime.status())["state"] == "ready"

    await runtime.check_candidate(Session(token="other"))
    assert store.snapshot() == original
    with pytest.raises(IntegrationError, match="invalid_response"):
        await runtime.check_candidate(Session(token="bad"))
    with pytest.raises(IntegrationError, match="verification_failed"):
        await runtime.install(Session(token="bad"))
    assert store.snapshot() == original
    await runtime.refresh()
    assert (await runtime.status())["state"] == "ready"


@pytest.mark.asyncio
async def test_refresh_reports_success_failure_and_recovery_without_provider_requests(tmp_path):
    config = IntegrationRegistry((IntegrationType("example", Settings, "https://example.test"),)).resolve(
        {"integrations": [{"id": "test", "type": "example", "state_dir": str(tmp_path)}]}
    )[0]
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session))
    runtime = ExampleIntegration(config, store)
    registry = CollectorRegistry()
    metrics = ProbeMetrics(registry, [config])
    runtime.bind_probe_reporter(metrics.reporter_for(config.type, config.id))
    await runtime.install(Session(token="installed"))

    await runtime.refresh(source=ProbeSource.PERIODIC)
    runtime.fail_refresh = True
    await runtime.refresh(source=ProbeSource.PERIODIC)
    runtime.fail_refresh = False
    await runtime.refresh(source=ProbeSource.PERIODIC)

    output = generate_latest(registry).decode("utf-8")
    identity = {"provider": "example", "instance": "test"}
    assert (
        sample_value(output, "platform_session_probe_total", identity | {"outcome": "success", "source": "periodic"})
        == 2
    )
    assert (
        sample_value(
            output, "platform_session_probe_total", identity | {"outcome": "invalid_response", "source": "periodic"}
        )
        == 1
    )
    assert sample_value(output, "platform_session_latest_probe_outcome", identity | {"outcome": "success"}) == 1
    last_success = sample_value(output, "platform_session_last_success_timestamp_seconds", identity)
    assert last_success is not None
    assert last_success > 0


if __name__ == "__main__":
    pytest_bazel.main()
