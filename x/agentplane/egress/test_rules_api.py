"""The transport-neutral boundary behind the stable agent-facing egress API."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_bazel

from x.agentplane.egress.policy import Index
from x.agentplane.egress.resources import ObjectMeta, Sandbox
from x.agentplane.egress.rules_api import HOST, PATH, RulesApi, RulesProjection, SandboxNotCurrentError

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
SANDBOX = Sandbox(metadata=ObjectMeta(name="sb", uid="sb-uid"))


def api() -> RulesApi:
    return RulesApi(RulesProjection(Index(sandboxes={"sb": SANDBOX}), clock=lambda: NOW))


def test_the_stable_host_and_response_contract_are_owned_by_the_api_boundary() -> None:
    rules = api()

    assert HOST == "agentplane-egress.agentplane-staging.svc.cluster.local"
    assert rules.serves(HOST.upper())
    response = rules.request(PATH, sandbox_name="sb", sandbox_uid="sb-uid")

    assert response.status == 200
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {"sandbox": "sb", "policies": []}


def test_the_api_exposes_no_other_path() -> None:
    response = api().request("/", sandbox_name="sb", sandbox_uid="sb-uid")

    assert (response.status, response.body, response.content_type) == (404, b"", "text/plain")


@pytest.mark.parametrize(("sandbox_name", "sandbox_uid"), [("gone", "sb-uid"), ("sb", "replaced-uid")])
def test_projection_requires_the_current_indexed_sandbox_identity(sandbox_name: str, sandbox_uid: str) -> None:
    with pytest.raises(SandboxNotCurrentError):
        api().request(PATH, sandbox_name=sandbox_name, sandbox_uid=sandbox_uid)


if __name__ == "__main__":
    pytest_bazel.main()
