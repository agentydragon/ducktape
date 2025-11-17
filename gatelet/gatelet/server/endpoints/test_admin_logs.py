from http import HTTPStatus
from pathlib import Path
import re

from httpx import AsyncClient

from gatelet.server.config import settings


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


async def test_view_logs(client: AsyncClient, tmp_path: Path, monkeypatch):
    log_file = tmp_path / "gatelet.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
    monkeypatch.setattr(settings.server, "log_file", str(log_file))

    session = await _login(client)
    response = await client.get("/admin/logs/", cookies={"admin_session": session})
    assert response.status_code == HTTPStatus.OK
    assert "line3" in response.text
    assert "line1" in response.text
