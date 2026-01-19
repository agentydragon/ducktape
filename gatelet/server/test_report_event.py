import pytest_bazel
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.reporter import send_event
from gatelet.server.models import WebhookIntegration, WebhookPayload
from gatelet.server.tests.utils import persist


async def test_send_event_works(client: AsyncClient, db_session: AsyncSession):
    integration = await persist(
        db_session,
        WebhookIntegration(
            name="report-test",
            description="Test integration",
            auth_type="none",
            auth_config={"type": "none"},
            is_enabled=True,
        ),
    )

    payload = {"foo": "bar"}
    result = await send_event("http://testserver", integration.name, payload, client=client)

    assert result["status"] == "ok"

    stmt = select(WebhookPayload).where(WebhookPayload.id == result["payload_id"])
    stored = (await db_session.execute(stmt)).scalar_one()
    assert stored.payload == payload


if __name__ == "__main__":
    pytest_bazel.main()
