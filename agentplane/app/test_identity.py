"""TokenReview rejection diagnostics distinguish authentication from app admission safely."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_bazel
from fastapi import HTTPException
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from agentplane.app.identity import TokenReviewer

TOKEN = "opaque-bearer-must-not-be-logged"
SUBJECT = "system:serviceaccount:testing:caller"
AUDIENCE = "app-audience"


@pytest.mark.parametrize(
    ("authenticated", "username", "audiences", "reason"),
    [
        (False, SUBJECT, [AUDIENCE], "unauthenticated"),
        (True, None, [AUDIENCE], "missing_username"),
        (True, SUBJECT, ["another-audience"], "wrong_audience"),
        (True, SUBJECT, [AUDIENCE], "subject_not_allowed"),
    ],
)
async def test_rejection_logs_reviewed_identity_and_fixed_reason_without_credentials(
    authenticated: bool, username: str | None, audiences: list[str], reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    authentication = AsyncMock(spec=AuthenticationV1Api)
    reviewed = k8s_client.V1TokenReview(
        spec=k8s_client.V1TokenReviewSpec(token=TOKEN, audiences=[AUDIENCE]),
        status=k8s_client.V1TokenReviewStatus(
            authenticated=authenticated,
            user=k8s_client.V1UserInfo(username=username),
            audiences=audiences,
            error=f"credential-bearing-error: {TOKEN}",
        ),
    )
    authentication.create_token_review = AsyncMock(return_value=reviewed)
    reviewer = TokenReviewer(cast(AuthenticationV1Api, authentication), audience=AUDIENCE, subjects=())

    with caplog.at_level(logging.INFO, logger="agentplane.app.identity"):
        if reason == "subject_not_allowed":
            with pytest.raises(HTTPException) as rejected:
                await reviewer.review(TOKEN)
            assert rejected.value.status_code == 403
        else:
            assert await reviewer.review(TOKEN) is None

    expected_username = repr(SUBJECT) if reason in {"wrong_audience", "subject_not_allowed"} else "None"
    assert [record.message for record in caplog.records] == [
        f"TokenReview rejected caller: reason={reason} username={expected_username}"
    ]
    assert TOKEN not in caplog.text
    assert "credential-bearing-error" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


if __name__ == "__main__":
    pytest_bazel.main()
