"""Launch and own the built Go egress proxy for the black-box tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

from sqlalchemy.engine import make_url

from agentplane.egress.settings import Settings
from agentplane.testing.go_process import GoProcess, go_process
from util.bazel.runfiles import get_required_path, own_repo_rlocation

_PROXY = get_required_path(own_repo_rlocation("agentplane/egress/cmd/egress-proxy_/egress-proxy"))
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/history_migrations/cmd/egress-history-migrate_/egress-history-migrate")
)
_GRACE_SECONDS = 55


def kubeconfig(path: Path, api_port: int) -> Path:
    path.write_text(
        dedent(f"""apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: http://127.0.0.1:{api_port}
contexts:
- name: test
  context:
    cluster: test
    user: test
current-context: test
users:
- name: test
  user: {{}}
""")
    )
    return path


@dataclass
class GoReplica:
    proxy_port: int
    process: GoProcess

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
    config = directory / f"go-replica-{settings.listen_port}.yaml"
    config.touch(mode=0o600)
    config.write_text(json.dumps({**settings.model_dump(mode="json", exclude_none=True), "database_url": database_url}))
    async with go_process(
        _PROXY,
        env={"AGENTPLANE_EGRESS_CONFIG_FILE": str(config)},
        stderr=config.with_suffix(".stderr"),
        health_port=settings.admin_port,
        grace_s=_GRACE_SECONDS,
    ) as process:
        yield GoReplica(settings.listen_port, process)
