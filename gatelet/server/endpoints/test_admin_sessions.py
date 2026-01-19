import re
from http import HTTPStatus

import pytest_bazel
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.models import AdminSession


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


async def test_list_admin_sessions(client: AsyncClient, db_session: AsyncSession):
    session_cookie = await _login(client)
    response = await client.get("/admin/admin-sessions/", cookies={"admin_session": session_cookie})
    assert response.status_code == HTTPStatus.OK
    assert session_cookie in response.text


async def test_invalidate_admin_session(client: AsyncClient, db_session: AsyncSession):
    session_cookie = await _login(client)
    response = await client.get("/admin/admin-sessions/", cookies={"admin_session": session_cookie})
    token = _extract_csrf(response.text)

    session_obj = (
        await db_session.execute(select(AdminSession).where(AdminSession.session_token == session_cookie))
    ).scalar_one()

    response = await client.post(
        f"/admin/admin-sessions/{session_obj.id}/invalidate",
        data={"csrf_token": token},
        cookies={"admin_session": session_cookie},
    )
    assert response.status_code == HTTPStatus.FOUND

    result = (
        await db_session.execute(select(AdminSession).where(AdminSession.id == session_obj.id))
    ).scalar_one_or_none()
    assert result is None


if __name__ == "__main__":
    pytest_bazel.main()
