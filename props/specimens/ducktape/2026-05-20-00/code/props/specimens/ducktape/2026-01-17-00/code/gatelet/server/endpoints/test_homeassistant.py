import re
from http import HTTPStatus

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


async def test_home_page(client: AsyncClient, test_auth_key):
    resp = await client.get(f"/k/{test_auth_key.key_value}/")
    assert resp.status_code == HTTPStatus.OK
    assert "sensor.test" in resp.text
    assert "test" in resp.text


async def test_entities_page(client: AsyncClient, test_auth_key):
    resp = await client.get(f"/k/{test_auth_key.key_value}/ha/")
    assert resp.status_code == HTTPStatus.OK
    assert "sensor.test" in resp.text


async def test_entity_detail(client: AsyncClient, test_auth_key):
    resp = await client.get(f"/k/{test_auth_key.key_value}/ha/sensor.test")
    assert resp.status_code == HTTPStatus.OK
    assert "sensor.test" in resp.text


async def test_entities_page_admin_links(client: AsyncClient, db_session: AsyncSession):
    home = await client.get("/")
    m = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert m
    token = m.group(1)
    resp = await client.post("/admin/login", data={"password": "gatelet", "csrf_token": token})
    session_cookie = resp.cookies["admin_session"]
    resp = await client.get("/admin/ha/", cookies={"session_token": session_cookie})
    assert resp.status_code == HTTPStatus.OK
    assert "homeassistant.local:8123" in resp.text
