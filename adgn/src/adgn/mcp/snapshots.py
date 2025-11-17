from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from mcp import types as mcp_types
from pydantic import BaseModel, Field


class McpServerState(StrEnum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    FAILED = "failed"


class InitializingServerEntry(BaseModel):
    state: Literal[McpServerState.INITIALIZING] = McpServerState.INITIALIZING


class RunningServerEntry(BaseModel):
    state: Literal[McpServerState.RUNNING] = McpServerState.RUNNING
    initialize: mcp_types.InitializeResult
    tools: list[mcp_types.Tool] = Field(default_factory=list)


class FailedServerEntry(BaseModel):
    state: Literal[McpServerState.FAILED] = McpServerState.FAILED
    error: str


ServerEntry = Annotated[InitializingServerEntry | RunningServerEntry | FailedServerEntry, Field(discriminator="state")]


class SamplingSnapshot(BaseModel):
    ts: str | None = None
    servers: dict[str, ServerEntry]
