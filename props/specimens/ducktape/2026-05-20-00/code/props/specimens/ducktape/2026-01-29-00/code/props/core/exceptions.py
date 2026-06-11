"""Shared exceptions for props agents."""

from __future__ import annotations

from uuid import UUID

from props.core.agent_types import AgentType


class AgentDidNotSubmitError(Exception):
    """Raised when an agent completes without calling submit()."""

    def __init__(self, agent_type: AgentType, agent_run_id: UUID):
        self.agent_type = agent_type
        self.agent_run_id = agent_run_id
        super().__init__(f"{agent_type} agent {agent_run_id} did not call submit()")
