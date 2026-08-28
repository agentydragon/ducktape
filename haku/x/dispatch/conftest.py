from collections.abc import AsyncIterator

import httpx
import pytest

from haku.x.dispatch import db
from haku.x.dispatch.app import AppResources, create_app
from haku.x.dispatch.config import Settings, ZoneConfig
from haku.x.dispatch.k8s_jobs import ZoneJobStamper
from haku.x.dispatch.litellm_keys import LiteLLMKeyClient
from haku.x.dispatch.models import ClassifierVerdict

# The fixture DATABASE_URL uses sqlite+aiosqlite://; SQLAlchemy loads the dialect
# at runtime, so gazelle cannot see the dependency.
# gazelle:include_dep @pypi//aiosqlite


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
        self, *, name: str, namespace: str, zone: str, model: str, prompt: str, litellm_key: str, result_token: str
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
        ANTHROPIC_BASE_URL="http://litellm.invalid:4000",
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

    async def classify(zone: str, prompt: str) -> ClassifierVerdict:
        return classifier_verdict

    zones = {"zai": ZoneConfig(namespace="haku-sandbox-zai", models={"glm-5.2-anthropic", "glm-5.1-anthropic"})}
    app = create_app(
        settings,
        AppResources(
            sessionmaker=db.make_sessionmaker(engine), stamper=stamper, keys=keys, classify=classify, zones=zones
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://validator") as client:
        yield client
    await engine.dispose()


@pytest.fixture
def haku_headers() -> dict[str, str]:
    return {"Authorization": "Bearer haku-token"}
