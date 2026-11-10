"""Pinned Codex executables, executor process, and isolated Postgres fixtures."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from python.runfiles import Runfiles
from sqlalchemy.engine import make_url

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.postgres import create_temporary_database


@dataclass(frozen=True)
class ExecutorServer:
    url: str
    process: asyncio.subprocess.Process


@pytest.fixture
def database_url(postgres_admin_url: str) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, "agentplane_test_" + uuid4().hex) as url:
        yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


@pytest.fixture
def codex_binary() -> str:
    return str(Runfiles.CreateOrRaise().root() / "agentplane_codex_cli_linux_x64/bin/codex")


@pytest.fixture
def code_mode_host() -> str:
    return str(Runfiles.CreateOrRaise().root() / "agentplane_codex_cli_linux_x64/bin/codex-code-mode-host")


@pytest.fixture
async def executor_server(codex_binary: str, tmp_path: Path) -> AsyncIterator[ExecutorServer]:
    with (undeclared_outputs_dir() / "executor.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            codex_binary,
            "exec-server",
            "--listen",
            "ws://127.0.0.1:0",
            env={**os.environ, "CODEX_HOME": str(tmp_path / "executor-home")},
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
        )
        try:
            assert process.stdout is not None
            line = await process.stdout.readline()
            # Upstream's CLI publishes its listener as one stdout line.
            yield ExecutorServer(line.decode().strip(), process)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.fixture
def executor_url(executor_server: ExecutorServer) -> str:
    return executor_server.url
