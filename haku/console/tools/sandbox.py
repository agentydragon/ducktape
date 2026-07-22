"""Haku Console's autonomous Kubernetes agent-sandbox MCP surface.

The three semantic tools deliberately hide Kubernetes object choreography from callers:
``reserve`` checks out a warm ``SandboxClaim``, ``exec`` renews its sliding lease and runs one
bounded argv command, and ``info`` reports whether the retained claim is ready, unhealthy, or
expired. They do not use the existing ``ws`` tool or its state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.exec.models import BaseExecResult, CMD_DESCRIPTION, TimeoutMs

HAKU_SANDBOX_SERVER_ID = "haku_sandbox"
HAKU_SANDBOX_TOOLS = frozenset({"reserve", "exec", "info"})

SandboxHandle = Annotated[
    str,
    Field(
        min_length=4,
        max_length=63,
        pattern=r"^hs-[a-z0-9]+$",
        description="Opaque handle returned by reserve (for example, hs-k7q2m).",
    ),
]


class SandboxInfo(BaseModel):
    """Current, caller-visible state of a retained sandbox claim."""

    model_config = ConfigDict(extra="forbid")

    handle: str
    state: Literal["allocating", "ready", "unhealthy", "expired", "not_found"]
    healthy: bool
    expires_at: datetime | None = Field(
        description="Current sliding lease deadline. Expired claims remain inspectable for seven days."
    )
    sandbox_name: str | None = None
    pod_name: str | None = None
    reason: str | None = None
    message: str | None = None


class SandboxExecResult(BaseExecResult):
    """Bounded command result plus the deadline renewed after the command completed."""

    expires_at: datetime


class AgentSandboxClient(Protocol):
    async def reserve(self) -> str: ...

    async def execute(
        self, *, handle: str, cmd: list[str], max_bytes: int, timeout_ms: int
    ) -> SandboxExecResult: ...

    async def info(self, handle: str) -> SandboxInfo: ...


def build_mcp(client: AgentSandboxClient) -> FastMCP:
    """Build the semantic sandbox server around an injected lifecycle client."""

    mcp: FastMCP = FastMCP(
        name=HAKU_SANDBOX_SERVER_ID,
        instructions=(
            "Reserve and use short-lived bash sandboxes from Haku's dedicated Kubernetes warm pool. "
            "All tools are autonomous (no operator approval). Handles have an eight-hour sliding lease: "
            "exec renews it before and after each command, while info reports retained expired claims."
        ),
    )

    @mcp.tool
    async def reserve() -> str:
        """Reserve a ready sandbox and return its short opaque handle."""

        return await client.reserve()

    @mcp.tool
    async def exec(
        handle: SandboxHandle,
        cmd: Annotated[list[str], Field(min_length=1, description=CMD_DESCRIPTION)],
        timeout_ms: Annotated[
            TimeoutMs,
            Field(description="Command timeout in milliseconds. The hard maximum is five minutes."),
        ],
        max_bytes: Annotated[
            int,
            Field(
                ge=0,
                le=100_000,
                description="Maximum bytes retained from each of stdout and stderr.",
            ),
        ] = 100_000,
    ) -> SandboxExecResult:
        """Execute an argv command in a reserved sandbox and renew its sliding lease."""

        return await client.execute(handle=handle, cmd=cmd, max_bytes=max_bytes, timeout_ms=timeout_ms)

    @mcp.tool
    async def info(handle: SandboxHandle) -> SandboxInfo:
        """Inspect claim, sandbox, and pod health; also distinguishes retained expired claims."""

        return await client.info(handle)

    return mcp
