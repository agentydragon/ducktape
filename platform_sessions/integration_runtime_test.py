"""The common lifecycle delegates provider checks without persisting rejected sessions."""

import pytest
import pytest_bazel
from pydantic import BaseModel, TypeAdapter

from platform_sessions.host import IntegrationError
from platform_sessions.integration_runtime import IntegrationRuntime
from platform_sessions.registry import IntegrationRegistry, IntegrationType
from platform_sessions.session_store import ProviderSessionStore


class Session(BaseModel):
    token: str


class Settings(BaseModel):
    pass


class ExampleIntegration(IntegrationRuntime[Session]):
    def parse(self, body: bytes) -> Session:
        return Session.model_validate_json(body)

    def verify(self, candidate: Session, *, transport=None) -> None:
        if candidate.token == "bad":
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


if __name__ == "__main__":
    pytest_bazel.main()
