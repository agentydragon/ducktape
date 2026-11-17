from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from agentplane.native.codex import driver, scenarios, wire
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import EFFORT, MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.async_process import AsyncNativeProcess


class ProbeRequest(BaseModel):
    id: str
    method: str
    params: dict[str, Any]


async def raw_request(process: AsyncNativeProcess, message: ProbeRequest | wire.InitializeRequest) -> dict[str, Any]:
    return (await process.request(message, matches=lambda frame: frame.get("id") == message.id)).frame


async def request(process: AsyncNativeProcess, method: str, **params: Any) -> dict[str, Any]:
    return await raw_request(process, ProbeRequest(id=str(uuid4()), method=method, params=params))


async def rpc(process: AsyncNativeProcess, method: str, **params: Any) -> dict[str, Any]:
    frame = await request(process, method, **params)
    assert "error" not in frame, frame
    result = frame["result"]
    assert isinstance(result, dict)
    return result


def native_probe_thread_start_params(
    codex: CodexHarness,
    base_instructions: str,
    *,
    ephemeral: bool,
    config_overrides: dict[str, Any] | None = None,
    history_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "cwd": str(codex.workspace),
        "model": MODEL,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "ephemeral": ephemeral,
        "baseInstructions": base_instructions,
        "config": {"model_reasoning_effort": EFFORT, **(config_overrides or {})},
        **({"historyMode": history_mode} if history_mode else {}),
    }


@asynccontextmanager
async def server(
    codex: CodexHarness, model: OpenAIResponses, name: str, *, experimental_api: bool = True
) -> AsyncIterator[AsyncNativeProcess]:
    endpoint = f"{model.origin}/v1"
    logs = codex.logs / name
    logs.mkdir()
    async with AsyncNativeProcess(
        logs,
        scenarios.command(codex.binary, endpoint=endpoint, max_retries=scenarios.MAX_RETRIES),
        cwd=codex.workspace,
        environment={
            **codex.base_environment,
            **scenarios.environment(endpoint=endpoint, token="test-key", codex_home=str(codex.codex_home)),
        },
    ) as process:
        frame = await raw_request(
            process, driver.initialize(str(uuid4()), client_name="agentplane-probe", experimental_api=experimental_api)
        )
        assert "error" not in frame, frame
        assert isinstance(frame["result"], dict)
        await process.send(wire.InitializedNotification())
        yield process
