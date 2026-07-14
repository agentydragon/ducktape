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


type ToolCallActor = OperatorActor | AgentActor
