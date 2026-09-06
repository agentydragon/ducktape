"""Identity and relay-client boundaries independent of Postgres."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import pytest_bazel

from x.agentplane.action_service.auth import KubernetesTokenAuthenticator
from x.agentplane.action_service.client import ActionServiceClient, ProjectedTokenFile
from x.agentplane.action_service.models import ActionRequestInput, PrincipalRole


class FakeTokenReviewApi:
    def __init__(self) -> None:
        self.reviews: dict[str, Any] = {}

    async def create_token_review(self, review: Any) -> Any:
        return self.reviews[review.spec.token]

    def issue(self, token: str, *, subject: str, audience: str) -> None:
        self.reviews[token] = SimpleNamespace(
            status=SimpleNamespace(
                authenticated=True, user=SimpleNamespace(username=subject), audiences=[audience], error=None
            )
        )


async def test_token_review_maps_only_named_subjects_and_roles() -> None:
    api = FakeTokenReviewApi()
    api.issue("caller", subject="system:serviceaccount:managed:relay", audience="agentplane-actions")
    api.issue("operator", subject="system:serviceaccount:haku:console", audience="agentplane-actions")
    api.issue("stranger", subject="system:serviceaccount:other:workload", audience="agentplane-actions")
    auth = KubernetesTokenAuthenticator(
        cast(Any, api),
        audience="agentplane-actions",
        caller_subjects=frozenset({"system:serviceaccount:managed:relay"}),
        operator_subjects=frozenset({"system:serviceaccount:haku:console"}),
    )

    caller = await auth.authenticate("caller")
    operator = await auth.authenticate("operator")
    assert caller is not None
    assert caller.role is PrincipalRole.CALLER
    assert operator is not None
    assert operator.role is PrincipalRole.OPERATOR
    assert await auth.authenticate("stranger") is None
    with pytest.raises(ValueError, match="both caller and operator"):
        KubernetesTokenAuthenticator(
            cast(Any, api),
            audience="agentplane-actions",
            caller_subjects=frozenset({"same"}),
            operator_subjects=frozenset({"same"}),
        )


async def test_trusted_projected_token_is_re_read_per_call_and_never_enters_the_body(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("first-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            202,
            json={
                "id": "00000000-0000-0000-0000-000000000001",
                "idempotency_key": "one",
                "capability": "agentplane:v0.echo",
                "arguments": {},
                "origin": {},
                "correlation": {},
                "caller_principal": None,
                "state": "decision_pending",
                "version": 1,
                "created_at": "2026-09-05T00:00:00Z",
                "updated_at": "2026-09-05T00:00:00Z",
                "decision": None,
                "execution": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://actions.example") as http:
        client = ActionServiceClient(http, ProjectedTokenFile(token_file))
        await client.submit(ActionRequestInput(idempotency_key="one", capability="agentplane:v0.echo", arguments={}))
        token_file.write_text("rotated-token")
        await client.submit(ActionRequestInput(idempotency_key="one", capability="agentplane:v0.echo", arguments={}))

    assert [request.headers["authorization"] for request in seen] == ["Bearer first-token", "Bearer rotated-token"]
    assert all(b"token" not in request.content for request in seen)


if __name__ == "__main__":
    pytest_bazel.main()
