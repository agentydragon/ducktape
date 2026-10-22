"""Launch and own the built Go egress proxy for the black-box tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy.engine import make_url

from agentplane.egress.settings import Settings
from agentplane.testing.go_process import GoProcess, TcpEndpoint, go_process, startup_object
from util.bazel.runfiles import get_required_path, own_repo_rlocation

_PROXY = get_required_path(own_repo_rlocation("agentplane/egress/cmd/egress-proxy_/egress-proxy"))
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/history_migrations/cmd/egress-history-migrate_/egress-history-migrate")
)
_GRACE_SECONDS = 55
_STARTUP_FILE_ENV = "AGENTPLANE_EGRESS_STARTUP_FILE"


@dataclass(frozen=True)
class EgressEndpoints:
    proxy: TcpEndpoint
    admin: TcpEndpoint
    agent_api: TcpEndpoint

    @classmethod
    def parse(cls, contents: bytes) -> EgressEndpoints:
        value = startup_object(contents)
        if value.keys() != {"proxy", "admin", "agent_api"}:
            raise ValueError("egress startup endpoints must contain proxy, admin, and agent_api")
        return cls(
            proxy=TcpEndpoint.parse(value["proxy"]),
            admin=TcpEndpoint.parse(value["admin"]),
            agent_api=TcpEndpoint.parse(value["agent_api"]),
        )


@dataclass
class GoReplica:
    endpoints: EgressEndpoints
    process: GoProcess[EgressEndpoints]

    @property
    def proxy_port(self) -> int:
        return self.endpoints.proxy.port

    def terminate(self) -> None:
        self.process.terminate()

    async def health(self, *, serving: bool) -> None:
        await self.process.health(serving=serving)


@asynccontextmanager
async def go_replica(settings: Settings, *, directory: Path) -> AsyncIterator[GoReplica]:
    """The Go proxy on `settings`, after the Goose migrator has taken over `settings.database_url`.

    The Go binary reads the same YAML keys the Python `Settings` model authors, so a key one side
    gains without the other is rejected here rather than ignored.
    """
    database_url = (
        make_url(settings.database_url)
        .set(drivername="postgresql", query={"sslmode": "disable"})
        .render_as_string(hide_password=False)
    )
    migrator = await asyncio.create_subprocess_exec(
        _MIGRATOR, "--database-url", database_url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    async with asyncio.timeout(60):
        output, _ = await migrator.communicate()
    assert migrator.returncode == 0, output.decode()
    with TemporaryDirectory(prefix="go-replica-", dir=directory) as private_directory:
        private = Path(private_directory)
        config = private / "settings.yaml"
        startup_file = private / "startup.json"
        config.touch(mode=0o600)
        config.write_text(
            json.dumps({**settings.model_dump(mode="json", exclude_none=True), "database_url": database_url})
        )
        async with go_process(
            _PROXY,
            env={"AGENTPLANE_EGRESS_CONFIG_FILE": str(config), _STARTUP_FILE_ENV: str(startup_file)},
            stderr=private / "egress.stderr",
            startup_file=startup_file,
            parse_endpoints=EgressEndpoints.parse,
            health_target=lambda endpoints: endpoints.admin.probe_target,
            grace_s=_GRACE_SECONDS,
        ) as process:
            yield GoReplica(process.endpoints, process)
