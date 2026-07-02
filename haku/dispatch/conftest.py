from collections.abc import AsyncIterator

import httpx
import pytest

from haku.dispatch import db
from haku.dispatch.app import AppResources, create_app
from haku.dispatch.config import Settings
from haku.dispatch.k8s_jobs import ZoneJobStamper
from haku.dispatch.litellm_keys import LiteLLMKeyClient
from haku.dispatch.models import ClassifierVerdict, Zone


def pytest_configure(config: pytest.Config) -> None:
    # The root conftest.py isn't in Bazel runfiles; mirror its asyncio auto
    # mode here (same pattern as mcp_infra/exec/conftest.py).
    config.option.asyncio_mode = "auto"


class FakeStamper(ZoneJobStamper):
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], dict] = {}

    async def job_exists(self, namespace: str, name: str) -> bool:
        return (namespace, name) in self.jobs

    async def create(
        self, *, name: str, namespace: str, zone: Zone, model: str, prompt: str, litellm_key: str, result_token: str
    ) -> None:
        self.jobs[(namespace, name)] = {
            "zone": zone,
            "model": model,
            "prompt": prompt,
            "litellm_key": litellm_key,
            "result_token": result_token,
        }

    async def delete(self, namespace: str, name: str) -> None:
        self.jobs.pop((namespace, name), None)


class FakeKeys(LiteLLMKeyClient):
    def __init__(self) -> None:
        self.minted: list[str] = []
        self.revoked: list[str] = []

    async def mint(self, job_id: str, models: list[str], max_budget_usd: float, ttl: str) -> str:
        self.minted.append(job_id)
        return f"sk-job-{job_id}"

    async def revoke(self, job_id: str) -> None:
        self.revoked.append(job_id)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite+aiosqlite://",
        WORKERS_LITELLM_MASTER_KEY="sk-master",
        ANTHROPIC_API_KEY="unused",
        HAKU_API_TOKEN="haku-token",
        RESULT_TOKEN_SECRET="hmac-secret",
    )


@pytest.fixture
def stamper() -> FakeStamper:
    return FakeStamper()


@pytest.fixture
def keys() -> FakeKeys:
    return FakeKeys()


@pytest.fixture
def classifier_verdict() -> ClassifierVerdict:
    return ClassifierVerdict(allowed=True, reason="generic public chore")


@pytest.fixture
async def client(
    settings: Settings, stamper: FakeStamper, keys: FakeKeys, classifier_verdict: ClassifierVerdict
) -> AsyncIterator[httpx.AsyncClient]:
    engine = db.make_engine(settings.database_url)
    await db.create_schema(engine)

    async def classify(zone: Zone, prompt: str) -> ClassifierVerdict:
        return classifier_verdict

    app = create_app(
        settings, AppResources(sessionmaker=db.make_sessionmaker(engine), stamper=stamper, keys=keys, classify=classify)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://validator") as client:
        yield client
    await engine.dispose()


@pytest.fixture
def haku_headers() -> dict[str, str]:
    return {"Authorization": "Bearer haku-token"}
