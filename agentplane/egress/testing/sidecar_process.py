"""Launch and own the built Go egress sidecar in Python end-to-end tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from uuid import uuid4

import yaml
from python.runfiles import Runfiles

from agentplane.egress.platform_routes import PLATFORM_ROUTES_LISTEN_ADDRESS, PlatformRoutes
from agentplane.egress.sidecar_config import READINESS_PATH, Proxy, Settings, WorkloadRoute
from agentplane.testing.go_process import HttpReadiness, go_process
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import Listener, listen_tcp

_BINARY = Runfiles.CreateOrRaise().root() / "ducktape/agentplane/egress/sidecar/cmd/egress-sidecar_/egress-sidecar"
_GRACE_SECONDS = 10


@dataclass(frozen=True)
class SidecarProcess:
    """Addresses of a running sidecar's listeners; they stay readable after it stops."""

    proxy_listener: Listener
    readiness_listener: Listener
    platform_route_listener: Listener


def _fd_address(listener: Listener) -> str:
    return f"fd:{listener.socket.fileno()}"


@asynccontextmanager
async def sidecar_process(
    *,
    proxy: Proxy,
    workload_routes: Mapping[str, WorkloadRoute] = MappingProxyType({}),
    platform_routes: PlatformRoutes | None = None,
) -> AsyncIterator[SidecarProcess]:
    routes = platform_routes or PlatformRoutes(listen_address=PLATFORM_ROUTES_LISTEN_ADDRESS)
    with ExitStack() as listeners, TemporaryDirectory(prefix="egress-sidecar-") as directory:
        sidecar = SidecarProcess(
            proxy_listener=listeners.enter_context(listen_tcp()),
            readiness_listener=listeners.enter_context(listen_tcp()),
            platform_route_listener=listeners.enter_context(listen_tcp()),
        )
        settings = Settings(
            proxy=proxy,
            workload_routes=dict(workload_routes),
            platform_routes=PlatformRoutes(
                listen_address=_fd_address(sidecar.platform_route_listener), routes=routes.routes
            ),
            listen_address=_fd_address(sidecar.proxy_listener),
            readiness_listen_address=_fd_address(sidecar.readiness_listener),
        )
        settings_path = Path(directory) / "settings.yaml"
        settings_path.write_text(yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True)))
        readiness = sidecar.readiness_listener
        async with go_process(
            _BINARY,
            env={Settings.config_file_env: str(settings_path)},
            stderr=undeclared_outputs_dir() / f"egress-sidecar-{uuid4().hex}.log",
            readiness=HttpReadiness(url=f"http://{readiness.host}:{readiness.port}{READINESS_PATH}"),
            grace_s=_GRACE_SECONDS,
            pass_fds=[
                listener.socket.fileno()
                for listener in (sidecar.proxy_listener, sidecar.readiness_listener, sidecar.platform_route_listener)
            ],
        ):
            # The child owns the inherited descriptors. Release the parent's copies so the
            # listeners stop accepting once the child exits.
            listeners.close()
            yield sidecar
