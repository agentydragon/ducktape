"""Launch and own the built Go egress proxy for the black-box tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import grpc
from python.runfiles import Runfiles

from agentplane.egress.settings import Settings
from agentplane.testing.go_process import GoProcess, go_process
from agentplane.workload_auth.listener import SPIFFEAuth
from util.db_url import render_postgresql_url
from util.net import Listener, listen_tcp

_PROXY = Runfiles.CreateOrRaise().root() / "ducktape/agentplane/egress/cmd/egress-proxy_/egress-proxy"
_GRACE_SECONDS = 55


@dataclass
class GoReplica:
    proxy_listener: Listener
    process: GoProcess
    admin_listener: Listener


@asynccontextmanager
async def go_replica(settings: Settings, *, directory: Path, tunnel_rules: bool = False) -> AsyncIterator[GoReplica]:
    """The Go proxy on `settings`, with normal startup initializing `settings.database_url`.

    The Go binary reads the same YAML keys the Python `Settings` model authors, so a key one side
    gains without the other is rejected here rather than ignored.
    """
    database_url = render_postgresql_url(settings.database_url, "postgresql", query={"sslmode": "disable"})
    with ExitStack() as listeners:
        proxy_listener = listeners.enter_context(listen_tcp())
        admin_listener = listeners.enter_context(listen_tcp())
        config = directory / f"go-replica-{proxy_listener.port}.yaml"
        config.touch(mode=0o600)
        settings = (
            settings.model_copy(
                update={
                    "tls_passthrough_targets": [*settings.tls_passthrough_targets, f"localhost:{admin_listener.port}"]
                }
            )
            if tunnel_rules
            else settings
        )
        config.write_text(
            json.dumps(
                {
                    **settings.model_dump(mode="json", exclude_none=True),
                    "database_url": database_url,
                    "forward": {
                        **settings.forward.model_dump(mode="json"),
                        "listen_address": f"fd:{proxy_listener.socket.fileno()}",
                    },
                    "admin_listen_address": f"fd:{admin_listener.socket.fileno()}",
                }
            )
        )
        health_credentials = None
        if settings.forward.tls is not None:
            roots = [method.ca_file for method in settings.forward.auth if isinstance(method, SPIFFEAuth)]
            if len(roots) != 1:
                raise ValueError("TLS proxy test health requires SPIFFE trust roots")
            health_credentials = grpc.ssl_channel_credentials(root_certificates=roots[0].read_bytes())
        async with go_process(
            _PROXY,
            env={"AGENTPLANE_EGRESS_CONFIG_FILE": str(config)},
            stderr=config.with_suffix(".stderr"),
            health_port=admin_listener.port,
            health_credentials=health_credentials,
            pass_fds=(proxy_listener.socket.fileno(), admin_listener.socket.fileno()),
            grace_s=_GRACE_SECONDS,
        ) as process:
            # The child inherited the listeners and has reported ready. Release the parent copies
            # before yielding so a later replica can safely reuse the descriptor numbers.
            listeners.close()
            yield GoReplica(proxy_listener, process, admin_listener)
