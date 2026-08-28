from __future__ import annotations

import datetime

import pytest
import pytest_bazel
from sqlalchemy import select

from haku.console.conftest import operator_id
from haku.console.database_schema import OAuthConnectionResultRow
from haku.console.oauth.connection_result import ConnectionSucceeded, PostgresConnectionResultStore


@pytest.fixture
def result_store(migrated_sessions, migrated_identity_store) -> PostgresConnectionResultStore:
    return PostgresConnectionResultStore(migrated_sessions, operator_identity_store=migrated_identity_store)


async def test_result_is_operator_bound_and_consumed_once(
    make_operator_client, migrated_sessions, result_store: PostgresConnectionResultStore
) -> None:
    with (
        make_operator_client(operator_external_user_key="result-owner") as owner,
        make_operator_client(operator_external_user_key="other-operator") as other,
    ):
        owner_id = await operator_id(migrated_sessions, "result-owner")
        result_id = await result_store.create(
            operator_id=owner_id,
            result=ConnectionSucceeded(
                title="Connected to Google Calendar", message="The account is now available in Haku Console."
            ),
        )

        wrong_operator = other.post(f"/api/oauth-results/{result_id}")
        consumed = owner.post(f"/api/oauth-results/{result_id}")
        replay = owner.post(f"/api/oauth-results/{result_id}")

    assert wrong_operator.status_code == 404
    assert consumed.json() == {
        "status": "success",
        "title": "Connected to Google Calendar",
        "message": "The account is now available in Haku Console.",
    }
    assert replay.status_code == 404


async def test_expired_result_is_not_returned(
    make_operator_client, migrated_sessions, result_store: PostgresConnectionResultStore
) -> None:
    with make_operator_client(operator_external_user_key="expired-result-owner") as owner:
        owner_id = await operator_id(migrated_sessions, "expired-result-owner")
        result_id = await result_store.create(
            operator_id=owner_id, result=ConnectionSucceeded(title="Connected", message="Ready.")
        )
        async with migrated_sessions.begin() as session:
            row = await session.scalar(
                select(OAuthConnectionResultRow).where(OAuthConnectionResultRow.result_id == result_id)
            )
            assert row is not None
            row.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)

        response = owner.post(f"/api/oauth-results/{result_id}")

    assert response.status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
