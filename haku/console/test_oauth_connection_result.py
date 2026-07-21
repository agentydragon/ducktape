from __future__ import annotations

import datetime

import pytest
import pytest_bazel
from sqlalchemy import select

from haku.console.conftest import console_sessions, operator_id, operator_identity_store
from haku.console.database_schema import OAuthConnectionResult as OAuthConnectionResultRow
from haku.console.oauth_connection_result import OAuthConnectionSucceeded, PostgresOAuthConnectionResultStore


@pytest.fixture
def result_store(migrated_db_url: str) -> PostgresOAuthConnectionResultStore:
    return PostgresOAuthConnectionResultStore(
        console_sessions(migrated_db_url), operator_identity_store=operator_identity_store(migrated_db_url)
    )


def test_result_is_operator_bound_and_consumed_once(
    make_operator_client, migrated_db_url: str, result_store: PostgresOAuthConnectionResultStore
) -> None:
    with (
        make_operator_client(operator_external_user_key="result-owner") as owner,
        make_operator_client(operator_external_user_key="other-operator") as other,
    ):
        owner_id = operator_id(migrated_db_url, "result-owner")
        result_id = result_store.create(
            operator_id=owner_id,
            result=OAuthConnectionSucceeded(
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


def test_expired_result_is_not_returned(
    make_operator_client, migrated_db_url: str, result_store: PostgresOAuthConnectionResultStore
) -> None:
    with make_operator_client(operator_external_user_key="expired-result-owner") as owner:
        owner_id = operator_id(migrated_db_url, "expired-result-owner")
        result_id = result_store.create(
            operator_id=owner_id, result=OAuthConnectionSucceeded(title="Connected", message="Ready.")
        )
        with console_sessions(migrated_db_url).begin() as session:
            row = session.scalar(
                select(OAuthConnectionResultRow).where(OAuthConnectionResultRow.result_id == result_id)
            )
            assert row is not None
            row.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)

        response = owner.post(f"/api/oauth-results/{result_id}")

    assert response.status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
