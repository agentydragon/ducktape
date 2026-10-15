"""Behavioral contract for the trusted MCP execution principal."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.mcp.principal import RequestPrincipal
from haku.console.tool_call_actor import AgentActor

AGENT_A = UUID("00000000-0000-4000-8000-000000000001")
AGENT_B = UUID("00000000-0000-4000-8000-000000000002")


def test_request_principal_is_immutable_and_rejects_untrusted_fields() -> None:
    principal = RequestPrincipal(agent_id=AGENT_A, access_profile_id=None)
    with pytest.raises(ValidationError, match="frozen"):
        principal.__setattr__("agent_id", AGENT_B)
    with pytest.raises(ValidationError, match="operator_id"):
        RequestPrincipal.model_validate(
            {"agent_id": str(AGENT_A), "access_profile_id": None, "operator_id": str(AGENT_B)}
        )
    with pytest.raises(ValidationError, match="access_profile_id"):
        RequestPrincipal(agent_id=AGENT_A, access_profile_id=" Public Coder ")


def test_request_principal_projects_the_authenticated_actor_and_drops_its_other_identity() -> None:
    actor = AgentActor(
        agent_id=AGENT_A, operator_id=UUID(int=7), binding_id=UUID(int=8), access_profile_id="public-coder"
    )
    assert RequestPrincipal.from_source(actor) == RequestPrincipal(agent_id=AGENT_A, access_profile_id="public-coder")


if __name__ == "__main__":
    pytest_bazel.main()
