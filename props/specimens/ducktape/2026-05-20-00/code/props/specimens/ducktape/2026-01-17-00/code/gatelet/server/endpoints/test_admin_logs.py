import re
from http import HTTPStatus
from pathlib import Path

from httpx import AsyncClient

from gatelet.server.config import Settings


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


async def test_view_logs(client: AsyncClient, test_settings: Settings):
    # test_settings fixture already injects settings via dependency override
    # Write test content to the log file configured in test_settings
    log_file = Path(test_settings.server.log_file)
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

    session = await _login(client)
    response = await client.get("/admin/logs/", cookies={"admin_session": session})
    assert response.status_code == HTTPStatus.OK
    assert "line3" in response.text
    assert "line1" in response.text
