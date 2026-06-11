"""Playwright end-to-end test for admin login and webhook navigation."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import pytest_bazel
import tomlkit
from testcontainers.postgres import PostgresContainer

from util.bazel.subprocess import python_env
from util.playwright import browser, page, playwright_sync  # noqa: F401
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.gatelet.manage import reset_db
from x.gatelet.server.config import (
    AdminSettings,
    AuthSettings,
    ChallengeResponseAuthSettings,
    DatabaseSettings,
    KeyInUrlAuthSettings,
    SecuritySettings,
    ServerSettings,
    Settings,
    WebhookSettings,
)
from x.gatelet.server.security import hash_password

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

TEST_ADMIN_PASSWORD = "gatelet"


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
    config.option.asyncio_default_fixture_loop_scope = "session"


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    """Session-scoped PostgreSQL container."""
    with PostgresContainer(image="postgres:16", username="postgres", password="postgres", dbname="gatelet") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """Async database URL from the testcontainer."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/gatelet"


@pytest.fixture(scope="session")
def test_settings(database_url: str) -> Settings:
    """Create test settings matching conftest pattern."""
    return Settings(
        database=DatabaseSettings(dsn=database_url),
        server=ServerSettings(host="127.0.0.1", port=8001, log_level="WARNING"),
        auth=AuthSettings(
            key_in_url=KeyInUrlAuthSettings(enabled=True, key_valid_days=365),
            challenge_response=ChallengeResponseAuthSettings(enabled=True, num_options=16),
        ),
        webhook=WebhookSettings(),
        admin=AdminSettings(password_hash=hash_password(TEST_ADMIN_PASSWORD)),
        security=SecuritySettings(csrf_secret="test-csrf-secret"),
    )


@pytest.fixture(scope="session")
def config_file(tmp_path_factory: pytest.TempPathFactory, test_settings: Settings) -> Path:
    """Write a gatelet.toml for the server subprocess from Pydantic settings."""
    path = tmp_path_factory.mktemp("gatelet") / "gatelet.toml"
    path.write_text(tomlkit.dumps(test_settings.model_dump()))
    return path


@pytest_asyncio.fixture(scope="session")
async def db_ready(database_url: str) -> AsyncGenerator[None]:
    """Create schema and populate sample data."""
    await reset_db(database_url)
    yield


@pytest.fixture(scope="session")
def server_url(config_file: Path, db_ready: None) -> Generator[str]:
    """Start the Gatelet server for browser tests."""
    out = undeclared_outputs_dir()
    port = 8001
    env = python_env(inherit=True)
    env["GATELET_CONFIG"] = str(config_file)
    server_log = (out / "server.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "x.gatelet.server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "debug",
        ],
        env=env,
        stdout=server_log,
        stderr=server_log,
    )
    time.sleep(1)
    url = f"http://127.0.0.1:{port}"
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait()
        server_log.close()


def test_admin_login_and_view_webhooks(page: Page, server_url: str) -> None:  # noqa: F811
    """Admin can log in via the UI and browse webhook payloads."""
    out = undeclared_outputs_dir()
    page.goto(f"{server_url}/")
    page.screenshot(path=out / "01_landing.png")
    page.fill('input[name="password"]', "gatelet")
    page.get_by_role("button", name="Login").click()
    page.screenshot(path=out / "02_after_login_click.png")
    page.wait_for_url(f"{server_url}/admin/", timeout=5000)
    page.screenshot(path=out / "03_admin_page.png")
    page.get_by_role("link", name="Webhooks").first.click()
    page.wait_for_url(f"{server_url}/admin/webhooks/")
    page.screenshot(path=out / "04_webhooks.png")
    assert "sample" in page.content()


if __name__ == "__main__":
    pytest_bazel.main()
