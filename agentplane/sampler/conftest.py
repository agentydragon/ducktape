"""Real sampler fixtures; the provider is the external scripted boundary."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.testing.codex_fixtures import ExecutorServer, code_mode_host, codex_binary, database_url
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.postgres_fixtures import postgres_admin_url, postgres_container, postgres_image


@pytest.fixture
async def executor_server() -> AsyncIterator[ExecutorServer]:
    """Serve the native execution backend through Runtime's typed proxy."""
    binary = (
        Runfiles.CreateOrRaise().root()
        / "ducktape/agentplane/env/runtime/execution/testing/typedproxy/typedproxy_bin_/typedproxy_bin"
    )
    with (undeclared_outputs_dir() / "sampler-native-executor.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            binary, "--listen", "127.0.0.1:0", stdout=asyncio.subprocess.PIPE, stderr=log
        )
        try:
            assert process.stdout is not None
            target = (await asyncio.wait_for(process.stdout.readline(), timeout=10)).decode().strip()
            if not target.startswith("127.0.0.1:"):
                raise RuntimeError(f"typed execution proxy did not publish its loopback target: {target!r}")
            yield ExecutorServer(target, process)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.fixture
async def execution_target(executor_server: ExecutorServer) -> str:
    return executor_server.url
