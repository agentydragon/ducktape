import re
from http import HTTPStatus

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.models import AuthCRSession


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


async def test_list_llm_sessions(client: AsyncClient, db_session: AsyncSession, test_auth_session: AuthCRSession):
    session_cookie = await _login(client)
    response = await client.get("/admin/llm-sessions/", cookies={"admin_session": session_cookie})
    assert response.status_code == HTTPStatus.OK
    assert test_auth_session.session_token in response.text


async def test_invalidate_llm_session(client: AsyncClient, db_session: AsyncSession, test_auth_session: AuthCRSession):
    session_cookie = await _login(client)
    response = await client.get("/admin/llm-sessions/", cookies={"admin_session": session_cookie})
    token = _extract_csrf(response.text)

    response = await client.post(
        f"/admin/llm-sessions/{test_auth_session.id}/invalidate",
        data={"csrf_token": token},
        cookies={"admin_session": session_cookie},
    )
    assert response.status_code == HTTPStatus.FOUND

    result = (
        await db_session.execute(select(AuthCRSession).where(AuthCRSession.id == test_auth_session.id))
    ).scalar_one_or_none()
    assert result is None
