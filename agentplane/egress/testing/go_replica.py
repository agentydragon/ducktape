"""Launch and own the built Go egress proxy for the black-box tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from agentplane.egress.settings import Settings
from agentplane.testing.go_process import GoProcess, go_process
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.net import Listener, listen_tcp

_PROXY = get_required_path(own_repo_rlocation("agentplane/egress/cmd/egress-proxy_/egress-proxy"))
_GRACE_SECONDS = 55


@dataclass
class GoReplica:
    proxy_listener: Listener
    process: GoProcess

    def terminate(self) -> None:
        self.process.terminate()

    async def health(self, *, serving: bool) -> None:
        await self.process.health(serving=serving)


@asynccontextmanager
async def go_replica(settings: Settings, *, directory: Path) -> AsyncIterator[GoReplica]:
    """The Go proxy on `settings`, with normal startup initializing `settings.database_url`.

    The Go binary reads the same YAML keys the Python `Settings` model authors, so a key one side
    gains without the other is rejected here rather than ignored.
    """
    database_url = (
        make_url(settings.database_url)
        .set(drivername="postgresql", query={"sslmode": "disable"})
        .render_as_string(hide_password=False)
    )
    with ExitStack() as listeners:
        proxy_listener = listeners.enter_context(listen_tcp())
        admin_listener = listeners.enter_context(listen_tcp())
        agent_api_listener = listeners.enter_context(listen_tcp())
        config = directory / f"go-replica-{proxy_listener.port}.yaml"
        config.touch(mode=0o600)
        config.write_text(
            json.dumps(
                {
                    **settings.model_dump(mode="json", exclude_none=True),
                    "database_url": database_url,
                    "listen_address": f"fd:{proxy_listener.socket.fileno()}",
                    "admin_listen_address": f"fd:{admin_listener.socket.fileno()}",
                    "agent_api_listen_address": f"fd:{agent_api_listener.socket.fileno()}",
                }
            )
        )
        async with go_process(
            _PROXY,
            env={"AGENTPLANE_EGRESS_CONFIG_FILE": str(config)},
            stderr=config.with_suffix(".stderr"),
            health_port=admin_listener.port,
            pass_fds=(
                proxy_listener.socket.fileno(),
                admin_listener.socket.fileno(),
                agent_api_listener.socket.fileno(),
            ),
            grace_s=_GRACE_SECONDS,
        ) as process:
            # The child inherited the listeners and has reported ready. Release the parent copies
            # before yielding so a later replica can safely reuse the descriptor numbers.
            listeners.close()
            yield GoReplica(proxy_listener, process)
