"""The `exact_actions` policy model: its reviewed names are the Action names it selects."""

from __future__ import annotations

from typing import Literal

from agentplane.action_service.models import PolicyKind
from agentplane.action_service.policies.kind import Kind


class ExactActions(Kind):
    type: Literal[PolicyKind.EXACT_ACTIONS]
