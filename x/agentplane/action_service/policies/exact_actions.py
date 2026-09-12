"""`exact_actions`: a listed Action matches by name alone."""

from __future__ import annotations

from typing import Literal

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import PolicyKind
from x.agentplane.action_service.policies.kind import Kind, Matched


class ExactActions(Kind):
    type: Literal[PolicyKind.EXACT_ACTIONS]


def evaluate(policy: ExactActions, action: ActionIdentity) -> Matched:
    """The registry has already checked that `action` is listed; nothing else is asked."""
    del policy
    return Matched(f"exact action {action.group}/{action.name} is listed")
