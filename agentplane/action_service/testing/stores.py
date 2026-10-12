"""An `ActionStore` for tests whose requests carry no external grant."""

from __future__ import annotations

from agentplane.action_service.connections import ConnectionAuthority
from agentplane.action_service.db import SessionMaker
from agentplane.action_service.store import ActionStore
from agentplane.action_service.testing.callers import admitted_callers


def action_store(sessions: SessionMaker) -> ActionStore:
    """Its connection authority admits no caller, so it authorizes no external grant."""
    return ActionStore(sessions, external_grants=ConnectionAuthority(sessions, admitted_callers()))
