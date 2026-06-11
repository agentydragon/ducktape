import re
from http import HTTPStatus

import pytest_bazel
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.models import AuthKey


def _extract_csrf(page_text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', page_text)
    assert m
    return m.group(1)


async def _login(client: AsyncClient) -> str:
    home = await client.get("/")
    token = _extract_csrf(home.text)
    response = await client.post("/admin/login", data={"password": "gatelet", "csrf_token": token})
    assert response.status_code == HTTPStatus.FOUND
    return response.cookies["admin_session"]


async def test_list_keys(client: AsyncClient, db_session: AsyncSession, test_auth_key: AuthKey):
    session = await _login(client)
    response = await client.get("/admin/keys/", cookies={"admin_session": session})
    assert response.status_code == HTTPStatus.OK
    assert str(test_auth_key.id) in response.text
    assert test_auth_key.key_value in response.text


async def test_create_key(client: AsyncClient, db_session: AsyncSession):
    session = await _login(client)
    response = await client.get("/admin/keys/new", cookies={"admin_session": session})
    token = _extract_csrf(response.text)

    count_before = await db_session.scalar(select(func.count()).select_from(AuthKey))

    response = await client.post(
        "/admin/keys/new", data={"description": "test key", "csrf_token": token}, cookies={"admin_session": session}
    )
    assert response.status_code == HTTPStatus.OK

    count_after = await db_session.scalar(select(func.count()).select_from(AuthKey))
    assert count_after == count_before + 1


async def test_revoke_key(client: AsyncClient, db_session: AsyncSession, test_auth_key: AuthKey):
    session = await _login(client)
    response = await client.get("/admin/keys/", cookies={"admin_session": session})
    token = _extract_csrf(response.text)

    response = await client.post(
        f"/admin/keys/{test_auth_key.id}/revoke", data={"csrf_token": token}, cookies={"admin_session": session}
    )
    assert response.status_code == HTTPStatus.FOUND

    await db_session.refresh(test_auth_key)
    assert test_auth_key.revoked_at is not None


if __name__ == "__main__":
    pytest_bazel.main()
