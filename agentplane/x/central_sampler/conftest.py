"""Real sampler fixtures; only provider and TokenReview are external scripted boundaries."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentplane.runner.testing.codex_exec_server_container import codex_exec_server_container
from agentplane.testing.codex_fixtures import (
    ExecutorServer,
    code_mode_host,
    codex_binary,
    database_url,
    executor_server,
)
from agentplane.testing.kubernetes_apiserver_fixtures import api_server, kubeconfig
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres_fixtures import postgres_admin_url, postgres_container, postgres_image
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture
async def execution_target(executor_server: ExecutorServer) -> AsyncIterator[str]:
    """Run the production typed adapter around the pinned native Codex exec-server."""
    adapter = get_required_path(own_repo_rlocation("agentplane/runner/execution/testing/adapter_bin"))
    with (undeclared_outputs_dir() / "sampler-execution-adapter.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            str(adapter),
            "--listen",
            "127.0.0.1:0",
            "--executor-url",
            executor_server.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
        )
        try:
            assert process.stdout is not None
            line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            target = line.decode().strip()
            if not target.startswith("127.0.0.1:"):
                raise RuntimeError(f"typed execution adapter did not publish its loopback target: {target!r}")
            yield target
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
