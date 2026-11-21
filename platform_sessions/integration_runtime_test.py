"""The common lifecycle delegates provider checks without persisting rejected sessions."""

import pytest
import pytest_bazel
from prometheus_client import CollectorRegistry
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
async def test_local_import_validation_failure_does_not_save_candidate(tmp_path):
    config = IntegrationRegistry((IntegrationType("example", Settings, "https://example.test"),)).resolve(
        {"integrations": [{"id": "test", "type": "example", "state_dir": str(tmp_path)}]}
    )[0]
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session))
    runtime = ExampleIntegration(config, store)
    good = runtime.parse(b'{"token":"good"}')
    await runtime.install(good)
    original = store.snapshot()

    # This verifier is local and has no upstream side effects.
    with pytest.raises(IntegrationError, match="verification_failed"):
        await runtime.install(Session(token="bad"))
    assert store.snapshot() == original
    await runtime.refresh()


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

    identity = {"provider": "example", "instance": "test"}
    assert (
        registry.get_sample_value(
            "platform_session_probe_total", identity | {"outcome": "success", "source": "periodic"}
        )
        == 2
    )
    assert (
        registry.get_sample_value(
            "platform_session_probe_total", identity | {"outcome": "invalid_response", "source": "periodic"}
        )
        == 1
    )
    assert registry.get_sample_value("platform_session_latest_probe_outcome", identity | {"outcome": "success"}) == 1
    last_success = registry.get_sample_value("platform_session_last_success_timestamp_seconds", identity)
    assert last_success is not None
    assert last_success > 0


if __name__ == "__main__":
    pytest_bazel.main()
