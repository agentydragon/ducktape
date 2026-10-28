"""Scripted provider and separate executor/proxy processes for the experiment."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from pydantic import BaseModel, ConfigDict

from agentplane.native.testing.model_endpoint import ModelEndpoint, ModelExchange
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

MODEL = "claude-sonnet-4-6"


class Request(BaseModel):
    model_config = ConfigDict(extra="allow")

    stream: bool


class Provider(ModelEndpoint[Request]):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    async def serve(self, request: web.Request) -> web.StreamResponse:
        self.headers.append(
            {
                name: request.headers[name]
                for name in ("User-Agent", "X-App", "Anthropic-Beta")
                if name in request.headers
            }
        )
        return await super().serve(request)

    def _parse_request(self, value: Any) -> Request:
        self.requests.append(value)
        return Request.model_validate(value)

    async def next(self) -> ModelExchange[Request]:
        async with asyncio.timeout(15):
            return await self._next_exchange()


@pytest.fixture
async def provider(request: pytest.FixtureRequest) -> AsyncIterator[Provider]:
    endpoint = Provider()
    await endpoint.start()
    try:
        yield endpoint
    finally:
        captures = undeclared_outputs_dir() / request.node.name
        captures.mkdir()
        (captures / "provider.json").write_text(
            json.dumps({"requests": endpoint.requests, "headers": endpoint.headers}, indent=2)
        )
        await endpoint.stop()


@asynccontextmanager
async def process(args: list[str], log: Path, *, env: dict[str, str]) -> AsyncIterator[asyncio.subprocess.Process]:
    with log.open("wb") as stderr:
        child = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=stderr, env=env, limit=1024 * 1024
        )
        try:
            yield child
        finally:
            if child.returncode is None:
                child.terminate()
            async with asyncio.timeout(5):
                await child.wait()


@pytest.fixture
async def executor(workspace: Path, base_environment: dict[str, str], native_logs: Path) -> AsyncIterator[str]:
    async with process(
        [
            str(get_required_path("_main/agentplane/native/claude/x/custom_sampler/executor_bin")),
            "--workspace",
            str(workspace),
        ],
        native_logs / "executor.log",
        env=base_environment,
    ) as child:
        assert child.stdout is not None
        async with asyncio.timeout(10):
            url = (await child.stdout.readline()).decode().strip()
        assert url.startswith("http://127.0.0.1:"), f"executor exited: {child.returncode}"
        yield url


@pytest.fixture
async def proxy(
    provider: Provider, tmp_path: Path, native_logs: Path, base_environment: dict[str, str]
) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = {
        "config-version": 8,
        "server": {"host": "127.0.0.1", "port": port},
        "management": {"disable-control-panel": True, "disable-auto-update-panel": True},
        "access": {"api-keys": ["sampler-test-client"]},
        "oauth": {"auth-dir": str(tmp_path / "auth")},
        "routing": {"retry": {"request-retry": 0}},
        "api-keys": {
            "claude": [
                {
                    "name": "scripted-claude",
                    "base-url": provider.origin,
                    "request-retry": 0,
                    "models": [{"name": MODEL}],
                    "keys": [{"api-key": "scripted-upstream-key", "fingerprint-profile": "claude-code-cli"}],
                }
            ]
        },
    }
    config_path = tmp_path / "proxy.json"
    config_path.write_text(json.dumps(config))  # JSON is valid YAML, the server's config format.
    async with process(
        [str(get_required_path(os.environ["PROXY_BINARY"])), "--config", str(config_path), "--local-model"],
        native_logs / "proxy.log",
        env={**base_environment, "GIN_MODE": "release"},
    ) as child:
        # Drain stdout to an artifact so proxy logs cannot fill its pipe during a request.
        assert child.stdout is not None

        async def drain() -> None:
            assert child.stdout is not None
            (native_logs / "proxy.stdout").write_bytes(await child.stdout.read())

        async with asyncio.TaskGroup() as tasks:
            drain_task = tasks.create_task(drain())
            origin = f"http://127.0.0.1:{port}"
            async with aiohttp.ClientSession() as client, asyncio.timeout(15):
                while True:
                    assert child.returncode is None, "proxy exited; inspect proxy.log"
                    try:
                        async with client.get(
                            origin + "/v1/models", headers={"Authorization": "Bearer sampler-test-client"}
                        ) as response:
                            if response.status == 200:
                                break
                    except aiohttp.ClientConnectorError:
                        pass  # The process is still starting, bounded by timeout above.
                    await asyncio.sleep(0.05)
            try:
                yield origin + "/v1/messages"
            finally:
                child.terminate()
                await child.wait()
                await drain_task
