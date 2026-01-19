"""Playwright end-to-end test for admin login and webhook navigation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_bazel
from playwright.sync_api import Page
from sqlalchemy.ext.asyncio import create_async_engine

from gatelet.manage import reset_db
from gatelet.server.models import Base

pytestmark = [pytest.mark.e2e, pytest.mark.requires_postgres]


@pytest.fixture(scope="session")
def server_url() -> Generator[str]:
    """Start the Gatelet server for browser tests."""
    cfg_dir = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.setdefault("GATELET_CONFIG", str(cfg_dir / "gatelet.toml"))
    database_url = env.get("DATABASE_URL")
    if database_url:
        engine = create_async_engine(database_url, future=True)

        async def _create_schema() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_create_schema())
        asyncio.run(engine.dispose())

    asyncio.run(reset_db(force=True))
    port = 8001
    proc = subprocess.Popen(
        [
            "python",
            "-m",
            "uvicorn",
            "gatelet.server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    time.sleep(1)
    url = f"http://127.0.0.1:{port}"
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait()


def test_admin_login_and_view_webhooks(page: Page, server_url: str) -> None:
    """Admin can log in via the UI and browse webhook payloads."""
    page.goto(f"{server_url}/")
    page.fill('input[name="password"]', "gatelet")
    page.click("text=Login")
    page.wait_for_url(f"{server_url}/admin/")
    page.click("text=Webhook Payloads")
    page.wait_for_url(f"{server_url}/admin/webhooks/")
    assert "sample" in page.content()


if __name__ == "__main__":
    pytest_bazel.main()
