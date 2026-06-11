"""Tests for webhook viewing endpoints."""

import re
from http import HTTPStatus

from hamcrest import all_of, assert_that, contains_string, is_not
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.models import WebhookIntegration, WebhookPayload
from gatelet.server.tests.utils import persist


def _extract_csrf(page_text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', page_text)
    assert m
    return m.group(1)


async def _admin_login(client: AsyncClient) -> str:
    home = await client.get("/")
    token = _extract_csrf(home.text)
    response = await client.post("/admin/login", data={"password": "gatelet", "csrf_token": token})
    assert response.status_code == HTTPStatus.FOUND
    return response.cookies["admin_session"]


async def test_list_all_payloads_key_auth(client: AsyncClient, db_session: AsyncSession, test_auth_key, monkeypatch):
    """Test listing all payloads with key path authentication."""
    # Create test integration and payloads
    integration = WebhookIntegration(
        name="test-view-integration",
        description="Test integration for viewing",
        auth_type="none",
        auth_config={"type": "none"},
        is_enabled=True,
    )
    integration = await persist(db_session, integration)

    # Add some test payloads
    payloads = []
    for i in range(15):
        payload = WebhookPayload(integration_id=integration.id, payload={"test": "data", "index": i, "value": i * 10})
        payloads.append(await persist(db_session, payload))

    # Use key-in-path authentication
    response = await client.get(f"/k/{test_auth_key.key_value}/webhooks/")
    assert response.status_code == HTTPStatus.OK

    # Check that the page contains the integration name and payload data
    assert_that(
        response.text,
        all_of(
            contains_string(integration.name),
            contains_string("test"),
            contains_string("data"),
            contains_string("page=2"),
        ),
    )


async def test_list_integration_payloads(client: AsyncClient, db_session: AsyncSession, test_auth_key):
    """Test listing integration-specific payloads."""
    # Create test integration and payloads
    integration = WebhookIntegration(
        name="test-specific-integration",
        description="Test integration for specific view",
        auth_type="none",
        auth_config={"type": "none"},
        is_enabled=True,
    )
    integration = await persist(db_session, integration)

    # Add some test payloads
    for i in range(5):
        payload = WebhookPayload(integration_id=integration.id, payload={"test": "specific", "index": i})
        await persist(db_session, payload)

    # Use key-in-path authentication
    response = await client.get(f"/k/{test_auth_key.key_value}/webhooks/{integration.name}")
    assert response.status_code == HTTPStatus.OK

    # Check that the page contains the integration name and specific data
    assert_that(
        response.text, all_of(contains_string(integration.name), contains_string("test"), contains_string("specific"))
    )


async def test_list_nonexistent_integration(client: AsyncClient, db_session: AsyncSession, test_auth_key):
    """Test listing a non-existent integration."""
    # Use key-in-path authentication
    response = await client.get(f"/k/{test_auth_key.key_value}/webhooks/nonexistent")
    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_session_auth_webhooks(client: AsyncClient, db_session: AsyncSession, test_auth_session):
    """Test listing payloads with session authentication."""
    # Create test integration and payloads
    integration = WebhookIntegration(
        name="test-session-integration",
        description="Test integration for session auth",
        auth_type="none",
        auth_config={"type": "none"},
        is_enabled=True,
    )
    integration = await persist(db_session, integration)

    # Add a test payload
    payload = WebhookPayload(integration_id=integration.id, payload={"test": "session", "value": 42})
    await persist(db_session, payload)

    # Use session-based authentication
    response = await client.get(f"/s/{test_auth_session.session_token}/webhooks/")
    assert response.status_code == HTTPStatus.OK

    # Check that the page contains the integration name and session data
    assert_that(
        response.text, all_of(contains_string(integration.name), contains_string("test"), contains_string("session"))
    )


async def test_disabled_payloads_hidden(client: AsyncClient, db_session: AsyncSession, test_auth_key):
    """Ensure payloads from disabled integrations are not shown."""
    integration = WebhookIntegration(
        name="disabled-int",
        description="Disabled integration",
        auth_type="none",
        auth_config={"type": "none"},
        is_enabled=False,
    )
    integration = await persist(db_session, integration)

    payload = WebhookPayload(integration_id=integration.id, payload={"hidden": True})
    await persist(db_session, payload)

    response = await client.get(f"/k/{test_auth_key.key_value}/webhooks/")
    assert response.status_code == HTTPStatus.OK
    assert_that(response.text, is_not(contains_string(integration.name)))


async def test_disabled_payloads_visible_admin(client: AsyncClient, db_session: AsyncSession):
    """Disabled integrations should be visible to admins."""
    integration = WebhookIntegration(
        name="disabled-admin",
        description="Disabled integration",
        auth_type="none",
        auth_config={"type": "none"},
        is_enabled=False,
    )
    await persist(db_session, integration)

    session_cookie = await _admin_login(client)
    response = await client.get("/admin/webhooks/", cookies={"session_token": session_cookie})
    assert response.status_code == HTTPStatus.OK
    assert integration.name in response.text
