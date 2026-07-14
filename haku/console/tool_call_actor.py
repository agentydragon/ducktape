"""Authenticated actors in the Haku Console tool-call domain."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperatorActor:
    operator_subject: str


@dataclass(frozen=True, slots=True)
class AgentActor:
    principal: str
    operator_subject: str


type ToolCallActor = OperatorActor | AgentActor
