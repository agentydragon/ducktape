"""Grocy container bring-up used by the eval CLI and the pytest fixtures."""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
from testcontainers.core.container import DockerContainer

from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from x.grocy_mcp.config import ServerSettings

logger = logging.getLogger(__name__)


def make_settings(grocy_url: str) -> ServerSettings:
    """Settings for a Grocy test instance: direct HTTP, no Authentik outpost."""
    return ServerSettings(grocy_url=grocy_url)


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


def configure_grocy_container(container: DockerContainer, *, data_dir: Path | None) -> None:
    """Apply the env / volume / port config every Grocy test container needs.

    If `data_dir` is provided, it's bind-mounted to Grocy's `/config/data`, so
    the SQLite DB lives at `data_dir/grocy.db` on the host throughout the run
    — no post-hoc copy needed. LinuxServer chowns the mount point on startup.
    """
    container.with_exposed_ports(80)
    container.with_env("PUID", "1000")
    container.with_env("PGID", "1000")
    container.with_env("TZ", "UTC")
    container.with_env("GROCY_MODE", "production")
    container.with_env("GROCY_DISABLE_AUTH", "true")
    container.with_volume_mapping(_prepare_custom_init_dir(), "/custom-cont-init.d", "ro")
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        container.with_volume_mapping(str(data_dir), "/config/data")


def grocy_url(container: DockerContainer) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(80)
    return f"http://{host}:{port}"


def wait_for_grocy_ready(container: DockerContainer, *, timeout_s: float = 90) -> None:
    """Poll `/api/system/info` until it returns 200; raise TimeoutError otherwise."""
    base_url = grocy_url(container)
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/", timeout=10)
            r = httpx.get(f"{base_url}/api/system/info", timeout=10)
            if r.status_code == 200:
                logger.info("Grocy ready at %s", base_url)
                return
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(2)
    raise TimeoutError(f"Grocy did not become ready at {base_url} within {timeout_s}s. Last: {last_err}")


@contextmanager
def run_grocy_container(*, data_dir: Path | None = None) -> Generator[DockerContainer]:
    """Run a fresh Grocy container with auth disabled; yield it once ready."""
    load_oci_image(GROCY)
    container = DockerContainer(GROCY.tag)
    configure_grocy_container(container, data_dir=data_dir)
    with container:
        wait_for_grocy_ready(container)
        yield container
