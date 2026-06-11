"""Domain exceptions for the agent server.

These exceptions are caught by FastAPI exception handlers and converted to
appropriate HTTP responses. This avoids repetitive try/except blocks in endpoints.
"""

from __future__ import annotations


class AgentNotFoundError(Exception):
    """Raised when an agent ID is not found in the registry."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent not found: {agent_id}")


class AgentSessionNotReadyError(Exception):
    """Raised when an agent container exists but session is not initialized."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent session not ready: {agent_id}")


class PolicyOperationError(Exception):
    """Raised when a policy operation fails (set, approve, reject, etc.)."""

    def __init__(self, operation: str, reason: str) -> None:
        self.operation = operation
        self.reason = reason
        super().__init__(f"Policy operation '{operation}' failed: {reason}")


class ApprovalNotFoundError(Exception):
    """Raised when a call_id is not found in pending approvals."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        super().__init__(f"Approval request not found: {call_id}")
