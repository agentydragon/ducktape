from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RunPhase(StrEnum):
    IDLE = "idle"
    SAMPLING = "sampling"
    WAITING_TOOL = "waiting_tool"
    TOOLS_RUNNING = "tools_running"
    WAITING_APPROVAL = "waiting_approval"
    SENDING_OUTPUT = "sending_output"
    ERROR = "error"


def determine_run_phase(*, pending_approvals: int, mcp_has_inflight: bool) -> RunPhase:
    """Precise run phase from live signals.

    - IDLE: no pending approvals and no MCP inflight
    - WAITING_APPROVAL: approvals pending
    - TOOLS_RUNNING: MCP policy gateway has in-flight requests
    - SAMPLING: has activity but not in other states
    """
    if pending_approvals > 0:
        return RunPhase.WAITING_APPROVAL
    if mcp_has_inflight:
        return RunPhase.TOOLS_RUNNING
    return RunPhase.IDLE


class AgentLifecycle(StrEnum):
    PERSISTED_ONLY = "persisted_only"
    STARTING = "starting"
    READY = "ready"


"""Status models and builder (no host volumes reported)."""


class UiStateLite(BaseModel):
    ready: bool
    model_config = ConfigDict(extra="forbid")


class ContainerState(BaseModel):
    present: bool
    id: str | None
    model_config = ConfigDict(extra="forbid")


class AgentStatusCore(BaseModel):
    id: str
    live: bool
    lifecycle: AgentLifecycle
    run_phase: RunPhase
    ui: UiStateLite
    container: ContainerState
    last_event_at: datetime | None = None
    model_config = ConfigDict(extra="forbid")
