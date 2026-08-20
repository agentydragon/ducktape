"""Authenticated actors in the Haku Console tool-call domain."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OperatorActor:
    operator_id: UUID


@dataclass(frozen=True, slots=True)
class AgentActor:
    agent_id: UUID
    operator_id: UUID
    binding_id: UUID
    # Persisted config-profile reference. ``None`` is the migration-safe, fail-closed default.
    access_profile_id: str | None = None
    # Present for a Console-launched sandbox. The session bearer then scopes reads/withdrawals to
    # this session and leaves an audit link on every tool call; external Agent credentials omit it.
    session_id: UUID | None = None


type ToolCallActor = OperatorActor | AgentActor
