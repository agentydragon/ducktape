"""Grocy container bring-up used by the eval CLI and the pytest fixtures."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed
from testcontainers.core.container import DockerContainer

from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from x.grocy_mcp.config import ServerSettings

logger = logging.getLogger(__name__)

# Required on gvisor sandboxes where IPv4 forwarding is off, so Docker
# port publishing is a no-op and `testcontainers.get_exposed_port(80)`
# never resolves. Outside the sandbox, the env var is absent and behaviour
# is unchanged.
_HOST_NETWORK_ENV = "GROCY_MCP_HOST_NETWORK"


def _host_network_enabled() -> bool:
    return os.environ.get(_HOST_NETWORK_ENV) == "1"


def make_settings(grocy_url: str) -> ServerSettings:
    """Settings for a Grocy test instance: direct HTTP, no Authentik outpost."""
    return ServerSettings(grocy_url=grocy_url)


@contextmanager
def grocy_custom_init_dir() -> Generator[str]:
    """Yield a tempdir containing an init script that strips IPv6 listen directives.

    LinuxServer s6-overlay runs scripts in /custom-cont-init.d/ after
    migrations (which generate the nginx config) but before services start.
    The dir is bind-mounted read-only into the container and removed on exit
    so repeated calls (e.g. from the eval CLI) don't leak under /tmp.
    """
    with tempfile.TemporaryDirectory(prefix="grocy-custom-init-") as d:
        script = Path(d) / "disable-ipv6.sh"
        script.write_text(
            "#!/bin/bash\n"
            "echo 'disable-ipv6: patching nginx configs'\n"
            "sed -i '/listen \\[/d' /config/nginx/site-confs/*.conf\n"
            "echo 'disable-ipv6: done, resulting config:'\n"
            "cat /config/nginx/site-confs/default.conf\n"
        )
        script.chmod(0o755)
        yield d


def configure_grocy_container(container: DockerContainer, *, init_dir: str, data_dir: Path | None) -> None:
    """Apply the env / volume / port config every Grocy test container needs.

    `init_dir` is the tempdir from `grocy_custom_init_dir()`; it must stay
    alive until the container exits. If `data_dir` is provided, it's
    bind-mounted to Grocy's `/config/data`, so the SQLite DB lives at
    `data_dir/grocy.db` on the host throughout the run — no post-hoc copy
    needed. LinuxServer chowns the mount point on startup.
    """
    if _host_network_enabled():
        container.with_kwargs(network_mode="host")
    else:
        container.with_exposed_ports(80)
    container.with_env("PUID", "1000")
    container.with_env("PGID", "1000")
    container.with_env("TZ", "UTC")
    container.with_env("GROCY_MODE", "production")
    container.with_env("GROCY_DISABLE_AUTH", "true")
    container.with_volume_mapping(init_dir, "/custom-cont-init.d", "ro")
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        container.with_volume_mapping(str(data_dir), "/config/data")


def grocy_url(container: DockerContainer) -> str:
    if _host_network_enabled():
        return "http://127.0.0.1:80"
    host = container.get_container_host_ip()
    port = container.get_exposed_port(80)
    return f"http://{host}:{port}"


class _NotReadyError(Exception):
    """Raised by the inner probe when Grocy replies but isn't finished migrating yet."""


def _probe_grocy_ready(base_url: str) -> None:
    """Hit `/` to force lazy migrations then verify the API is serving.

    Grocy runs its SQLite migrations lazily on the first HTTP request to
    `/`; before that, `/api/*` endpoints hit the auth middleware and
    SELECT from the `users` table, which doesn't exist yet (500 "no such
    table: users"). Raises `_NotReadyError` — meant to be retried — when Grocy
    is reachable but the API isn't serving JSON yet.
    """
    httpx.get(f"{base_url}/", timeout=10)
    r = httpx.get(f"{base_url}/api/objects/locations", timeout=10)
    if r.status_code != 200:
        raise _NotReadyError(f"HTTP {r.status_code}: {r.text[:120]!r}")
    try:
        body = r.json()
    except ValueError as e:
        raise _NotReadyError(f"HTTP 200 but non-JSON body ({e}): {r.text[:120]!r}") from e
    if not isinstance(body, list):
        raise _NotReadyError(f"HTTP 200 but unexpected body type {type(body).__name__}: {r.text[:120]!r}")


def wait_for_grocy_ready(container: DockerContainer, *, timeout_s: float = 60) -> None:
    """Poll until Grocy has migrated the DB and is serving API requests.

    Raises whatever the last probe attempt raised once `timeout_s` elapses
    (`reraise=True`), so the caller sees the real cause (HTTP status,
    JSON decode error, connection refused) rather than a generic timeout.
    """
    base_url = grocy_url(container)
    for attempt in Retrying(
        stop=stop_after_delay(timeout_s),
        wait=wait_fixed(1),
        retry=retry_if_exception_type((_NotReadyError, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)),
        reraise=True,
    ):
        with attempt:
            _probe_grocy_ready(base_url)
    logger.info("Grocy ready at %s", base_url)


@contextmanager
def run_grocy_container(*, data_dir: Path | None = None) -> Generator[DockerContainer]:
    """Run a fresh Grocy container with auth disabled; yield it once ready."""
    load_oci_image(GROCY)
    with grocy_custom_init_dir() as init_dir:
        container = DockerContainer(GROCY.tag)
        configure_grocy_container(container, init_dir=init_dir, data_dir=data_dir)
        with container:
            wait_for_grocy_ready(container)
            yield container
