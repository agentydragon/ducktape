"""Behavioral contract for shared grant-principal and request-principal vocabulary."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from haku.console.grants.principal import (
    AgentGrantPrincipal,
    GrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
    grant_principal_applies_to,
)
from haku.console.tool_call_actor import AgentActor

AGENT_A = UUID("00000000-0000-4000-8000-000000000001")
AGENT_B = UUID("00000000-0000-4000-8000-000000000002")
SESSION_A = UUID("10000000-0000-4000-8000-000000000001")
SESSION_B = UUID("10000000-0000-4000-8000-000000000002")
GRANT_PRINCIPAL_ADAPTER: TypeAdapter[GrantPrincipal] = TypeAdapter(GrantPrincipal)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kind": "agent", "agent_id": str(AGENT_A)}, AgentGrantPrincipal(agent_id=AGENT_A)),
        ({"kind": "session", "session_id": str(SESSION_A)}, SessionGrantPrincipal(session_id=SESSION_A)),
    ],
)
def test_grant_principal_variants_round_trip_json(payload: dict[str, str], expected: GrantPrincipal) -> None:
    principal = GRANT_PRINCIPAL_ADAPTER.validate_python(payload)
    assert principal == expected
    assert principal.model_dump(mode="json") == payload


def test_principal_kinds_default_for_internal_construction() -> None:
    assert AgentGrantPrincipal(agent_id=AGENT_A).kind is GrantPrincipalKind.AGENT
    assert SessionGrantPrincipal(session_id=SESSION_A).kind is GrantPrincipalKind.SESSION


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"kind": "agent"},
        {"kind": "session"},
        {"kind": "other", "agent_id": str(AGENT_A)},
        {"kind": "agent", "agent_id": str(AGENT_A), "session_id": str(SESSION_A)},
        {"kind": "session", "session_id": str(SESSION_A), "agent_id": str(AGENT_A)},
    ],
)
def test_grant_principal_wire_shapes_fail_closed(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        GRANT_PRINCIPAL_ADAPTER.validate_python(payload)


def test_grant_and_request_principals_are_immutable_and_reject_untrusted_fields() -> None:
    principal = AgentGrantPrincipal(agent_id=AGENT_A)
    with pytest.raises(ValidationError, match="frozen"):
        principal.__setattr__("agent_id", AGENT_B)
    with pytest.raises(ValidationError, match="operator_id"):
        RequestPrincipal.model_validate({"agent_id": str(AGENT_A), "operator_id": str(AGENT_B)})
    with pytest.raises(ValidationError, match="access_profile_id"):
        RequestPrincipal(agent_id=AGENT_A, access_profile_id=" Public Coder ", session_id=None)


def test_request_principal_projects_the_authenticated_actor_and_drops_its_other_identity() -> None:
    actor = AgentActor(
        agent_id=AGENT_A,
        operator_id=UUID(int=7),
        binding_id=UUID(int=8),
        access_profile_id="public-coder",
        session_id=SESSION_A,
    )
    assert RequestPrincipal.from_source(actor) == RequestPrincipal(
        agent_id=AGENT_A, session_id=SESSION_A, access_profile_id="public-coder"
    )


def test_agent_grant_principal_covers_every_authenticated_execution_of_that_agent() -> None:
    principal = AgentGrantPrincipal(agent_id=AGENT_A)

    assert grant_principal_applies_to(
        principal, RequestPrincipal(agent_id=AGENT_A, session_id=None, access_profile_id=None)
    )
    assert grant_principal_applies_to(
        principal, RequestPrincipal(agent_id=AGENT_A, session_id=SESSION_A, access_profile_id=None)
    )
    assert not grant_principal_applies_to(
        principal, RequestPrincipal(agent_id=AGENT_B, session_id=SESSION_B, access_profile_id=None)
    )


def test_session_grant_principal_covers_only_the_exact_live_session_identity() -> None:
    principal = SessionGrantPrincipal(session_id=SESSION_A)

    assert grant_principal_applies_to(
        principal, RequestPrincipal(agent_id=AGENT_A, session_id=SESSION_A, access_profile_id=None)
    )
    assert not grant_principal_applies_to(
        principal, RequestPrincipal(agent_id=AGENT_A, session_id=None, access_profile_id=None)
    )
    assert not grant_principal_applies_to(
        principal, RequestPrincipal(agent_id=AGENT_A, session_id=SESSION_B, access_profile_id=None)
    )


if __name__ == "__main__":
    pytest_bazel.main()
