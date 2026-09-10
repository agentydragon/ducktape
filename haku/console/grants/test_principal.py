"""Behavioral contract for shared grant-principal and request-principal vocabulary."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from haku.console.grants.principal import (
    AccessProfileGrantPrincipal,
    AgentGrantPrincipal,
    GrantPrincipal,
    GrantPrincipalInput,
    GrantPrincipalKind,
    RequestPrincipal,
    grant_principal_applies_to,
    resolve_grant_principal_input,
)
from haku.console.tool_call_actor import AgentActor

AGENT_A = UUID("00000000-0000-4000-8000-000000000001")
AGENT_B = UUID("00000000-0000-4000-8000-000000000002")
GRANT_PRINCIPAL_ADAPTER: TypeAdapter[GrantPrincipal] = TypeAdapter(GrantPrincipal)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kind": "agent", "agent_id": str(AGENT_A)}, AgentGrantPrincipal(agent_id=AGENT_A)),
        (
            {"kind": "access_profile", "access_profile_id": "public-coder"},
            AccessProfileGrantPrincipal(access_profile_id="public-coder"),
        ),
    ],
)
def test_grant_principal_variants_round_trip_json(payload: dict[str, str], expected: GrantPrincipal) -> None:
    principal = GRANT_PRINCIPAL_ADAPTER.validate_python(payload)
    assert principal == expected
    assert principal.model_dump(mode="json") == payload


def test_principal_kinds_default_for_internal_construction() -> None:
    assert AgentGrantPrincipal(agent_id=AGENT_A).kind is GrantPrincipalKind.AGENT
    assert AccessProfileGrantPrincipal(access_profile_id="public-coder").kind is GrantPrincipalKind.ACCESS_PROFILE


@pytest.mark.parametrize("payload", [{}, {"kind": "agent"}, {"kind": "other", "agent_id": str(AGENT_A)}])
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
        RequestPrincipal(agent_id=AGENT_A, access_profile_id=" Public Coder ")


def test_request_principal_projects_the_authenticated_actor_and_drops_its_other_identity() -> None:
    actor = AgentActor(
        agent_id=AGENT_A, operator_id=UUID(int=7), binding_id=UUID(int=8), access_profile_id="public-coder"
    )
    assert RequestPrincipal.from_source(actor) == RequestPrincipal(agent_id=AGENT_A, access_profile_id="public-coder")


def test_agent_grant_principal_covers_every_authenticated_execution_of_that_agent() -> None:
    principal = AgentGrantPrincipal(agent_id=AGENT_A)

    assert grant_principal_applies_to(principal, RequestPrincipal(agent_id=AGENT_A, access_profile_id=None))
    assert not grant_principal_applies_to(principal, RequestPrincipal(agent_id=AGENT_B, access_profile_id=None))


def test_access_profile_grant_principal_covers_agents_assigned_to_that_profile() -> None:
    principal = AccessProfileGrantPrincipal(access_profile_id="public-coder")

    assert grant_principal_applies_to(principal, RequestPrincipal(agent_id=AGENT_A, access_profile_id="public-coder"))
    assert not grant_principal_applies_to(principal, RequestPrincipal(agent_id=AGENT_A, access_profile_id="other"))


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("self", AgentGrantPrincipal(agent_id=AGENT_A)),
        (AgentGrantPrincipal(agent_id=AGENT_B), AgentGrantPrincipal(agent_id=AGENT_B)),
        (
            AccessProfileGrantPrincipal(access_profile_id="other"),
            AccessProfileGrantPrincipal(access_profile_id="other"),
        ),
    ],
)
def test_grant_principal_input_accepts_any_explicit_identity_or_self(
    requested: GrantPrincipalInput, expected: GrantPrincipal
) -> None:
    request_principal = RequestPrincipal(agent_id=AGENT_A, access_profile_id="public-coder")

    assert resolve_grant_principal_input(requested, request_principal) == expected


if __name__ == "__main__":
    pytest_bazel.main()
