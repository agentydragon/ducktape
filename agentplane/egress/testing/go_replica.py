"""Launch and own the built Go egress proxy for the black-box tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import aiohttp
from sqlalchemy.engine import make_url
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.egress.settings import Settings
from util.bazel.runfiles import get_required_path, own_repo_rlocation

_PROXY = get_required_path(own_repo_rlocation("agentplane/egress/cmd/egress-proxy_/egress-proxy"))
_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/egress/history_migrations/cmd/egress-history-migrate_/egress-history-migrate")
)
_GRACE_SECONDS = 55

# Keys of `Settings` only the Python proxy reads: the Go binary rejects keys it does not know.
_PYTHON_ONLY = {"confdir", "decision_flush_seconds"}


async def expect_health(
    admin_port: int, status: int, *, alive: Callable[[], bool], exit_description: Callable[[], str]
) -> None:
    """Until the admin listener on `admin_port` answers `/healthz` with `status`; fails at once if the proxy died."""
    async with aiohttp.ClientSession() as client:
        async for attempt in AsyncRetrying(stop=stop_after_delay(20), wait=wait_fixed(0.05), reraise=True):
            with attempt:
                assert alive(), f"proxy exited with {exit_description()}"
                async with client.get(f"http://127.0.0.1:{admin_port}/healthz") as response:
                    assert response.status == status
                    await response.read()
                    return
    raise AssertionError("health retry exhausted")


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
