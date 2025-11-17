"""Tests for admin password authentication."""

import re

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


async def test_admin_login_success(client: AsyncClient, db_session: AsyncSession):
    home = await client.get("/")
    m = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert m
    token = m.group(1)
    response = await client.post("/admin/login", data={"password": "gatelet", "csrf_token": token})
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/"
    assert "admin_session" in response.cookies


async def test_admin_login_invalid(client: AsyncClient, db_session: AsyncSession):
    home = await client.get("/")
    m = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert m
    token = m.group(1)
    response = await client.post("/admin/login", data={"password": "wrong", "csrf_token": token})
    assert response.status_code == 401
