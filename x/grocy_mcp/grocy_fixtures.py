"""Shared Grocy container fixtures for tests and evals."""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from x.grocy_mcp.config import ServerSettings

logger = logging.getLogger(__name__)


def make_settings(grocy_url: str) -> ServerSettings:
    return ServerSettings(
        oidc_issuer="https://auth.example.com/application/o/grocy-mcp/",
        oidc_client_id="unused",
        oidc_client_secret="unused",
        public_base_url="https://grocy-mcp.example.com",
        grocy_url=grocy_url,
        grocy_proxy_client_id="unused",
    )


def _prepare_custom_init_dir() -> str:
    """Create a dir with a script that strips IPv6 listen directives from nginx config.

    LinuxServer s6-overlay runs scripts in /custom-cont-init.d/ after
    migrations (which generate the nginx config) but before services start.
    """
    init_dir = tempfile.mkdtemp(prefix="grocy-custom-init-")
    script = Path(init_dir) / "disable-ipv6.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo 'disable-ipv6: patching nginx configs'\n"
        "sed -i '/listen \\[/d' /config/nginx/site-confs/*.conf\n"
        "echo 'disable-ipv6: done, resulting config:'\n"
        "cat /config/nginx/site-confs/default.conf\n"
    )
    script.chmod(0o755)
    return init_dir


@pytest.fixture(scope="session", autouse=True)
def _preload_grocy() -> None:
    load_oci_image(GROCY)


@pytest.fixture(scope="session")
def grocy_container() -> Generator[LoggedContainer]:
    """Session-scoped Grocy container with auth disabled."""
    container = LoggedContainer(GROCY.tag, test_name="grocy")
    container.with_exposed_ports(80)
    container.with_env("PUID", "1000")
    container.with_env("PGID", "1000")
    container.with_env("TZ", "UTC")
    container.with_env("GROCY_MODE", "production")
    container.with_env("GROCY_DISABLE_AUTH", "true")
    container.with_volume_mapping(_prepare_custom_init_dir(), "/custom-cont-init.d", "ro")

    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(80)
        base_url = f"http://{host}:{port}"

        deadline = time.monotonic() + 90
        last_err = ""
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{base_url}/", timeout=10)
                r = httpx.get(f"{base_url}/api/system/info", timeout=10)
                if r.status_code == 200:
                    logger.info("Grocy ready at %s", base_url)
                    break
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(2)
        else:
            raise TimeoutError(f"Grocy did not become ready at {base_url} within 90s. Last: {last_err}")

        yield container


@pytest.fixture(scope="session")
def grocy_base_url(grocy_container: LoggedContainer) -> str:
    host = grocy_container.get_container_host_ip()
    port = grocy_container.get_exposed_port(80)
    return f"http://{host}:{port}"
