"""Launch and own the built Go egress proxy, with the surface of `replica` for the same black-box tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from agentplane.egress.settings import Settings
from agentplane.egress.testing.replica import expect_health
from util.bazel.runfiles import get_required_path, own_repo_rlocation

_PROXY = get_required_path(own_repo_rlocation("agentplane/egress/go/cmd/egress-proxy_/egress-proxy"))
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/go/migrations/cmd/egress-history-migrate_/egress-history-migrate")
)
_GRACE_SECONDS = 55

# Settings the Go binary has no use for: mitmproxy's working directory, and a flush interval the Go
# history log replaces with a fixed one.
_PYTHON_ONLY = {"confdir", "decision_flush_seconds"}


@dataclass
class GoReplica:
    proxy_port: int
    admin_port: int
    process: asyncio.subprocess.Process
    stderr: Path

    def terminate(self) -> None:
        self.process.terminate()

    async def health(self, status: int) -> None:
        await expect_health(
            self.admin_port,
            status,
            alive=lambda: self.process.returncode is None,
            exit_description=lambda: f"{self.process.returncode}:\n{self.stderr.read_text()}",
        )


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
    config.write_text(
        json.dumps(
            {**settings.model_dump(mode="json", exclude=_PYTHON_ONLY, exclude_none=True), "database_url": database_url}
        )
    )
    stderr = config.with_suffix(".stderr")
    with stderr.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            _PROXY, env={"AGENTPLANE_EGRESS_CONFIG_FILE": str(config)}, stdout=log, stderr=log
        )
    running = GoReplica(settings.listen_port, settings.admin_port, process, stderr)
    try:
        await running.health(200)
        yield running
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            async with asyncio.timeout(_GRACE_SECONDS):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AssertionError("proxy did not terminate within its grace budget") from None
        assert process.returncode == 0, stderr.read_text()
