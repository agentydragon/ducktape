"""The operator-browser API for Web Push subscriptions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel

from haku.console.conftest import TEST_WEB_PUSH

_SUBSCRIPTION = {"endpoint": "https://push.test/endpoint/a", "p256dh": "public-key", "auth": "auth-secret"}


@pytest.fixture
def push_client(make_operator_client: Callable[..., Any]) -> Any:
    return lambda **overrides: make_operator_client(web_push=TEST_WEB_PUSH, **overrides)


def test_config_publishes_the_application_server_key(push_client: Any) -> None:
    with push_client() as client:
        response = client.get("/api/push/config")

    assert response.status_code == 200
    assert response.json()["application_server_key"]


def test_config_reports_null_rather_than_failing_when_push_is_unconfigured(
    make_operator_client: Callable[..., Any],
) -> None:
    """The UI has to be able to say "this console cannot notify you" — a 503 here reads as an
    outage instead of a deployment that simply has no VAPID key."""
    with make_operator_client() as client:
        response = client.get("/api/push/config")

    assert response.status_code == 200
    assert response.json()["application_server_key"] is None


def test_subscribing_is_rejected_when_push_is_unconfigured(make_operator_client: Callable[..., Any]) -> None:
    with make_operator_client() as client:
        response = client.post("/api/push/subscriptions", json=_SUBSCRIPTION)

    assert response.status_code == 503


def test_subscription_round_trips_and_records_the_browser(push_client: Any) -> None:
    with push_client() as client:
        assert (
            client.post("/api/push/subscriptions", json=_SUBSCRIPTION, headers={"User-Agent": "Firefox"}).status_code
            == 204
        )

        listed = client.get("/api/push/subscriptions").json()
        assert [entry["endpoint"] for entry in listed] == [_SUBSCRIPTION["endpoint"]]
        assert listed[0]["user_agent"] == "Firefox"

        assert (
            client.request(
                "DELETE", "/api/push/subscriptions", params={"endpoint": _SUBSCRIPTION["endpoint"]}
            ).status_code
            == 204
        )
        assert client.get("/api/push/subscriptions").json() == []


def test_unsubscribing_an_unknown_endpoint_is_not_found(push_client: Any) -> None:
    with push_client() as client:
        response = client.request("DELETE", "/api/push/subscriptions", params={"endpoint": "https://push.test/nope"})

    assert response.status_code == 404


def test_the_push_surface_is_operator_only(make_client: Callable[..., Any]) -> None:
    """Anonymous callers get nothing here — a subscription says where to reach an Operator."""
    with make_client(web_push=TEST_WEB_PUSH) as client:
        assert client.get("/api/push/config").status_code == 401
        assert client.get("/api/push/subscriptions").status_code == 401
        assert client.post("/api/push/subscriptions", json=_SUBSCRIPTION).status_code == 401


def test_mutations_require_the_consoles_exact_origin(push_client: Any) -> None:
    """Same guard as every other operator mutation: the framed haku-ui origin cannot ride the
    operator's session cookie to register a push endpoint of its choosing."""
    with push_client() as client:
        response = client.post(
            "/api/push/subscriptions", json=_SUBSCRIPTION, headers={"Origin": "https://haku-ui.test"}
        )

    assert response.status_code == 403


def test_a_user_agent_is_truncated_rather_than_stored_unbounded(push_client: Any) -> None:
    with push_client() as client:
        client.post("/api/push/subscriptions", json=_SUBSCRIPTION, headers={"User-Agent": "u" * 900})
        stored = client.get("/api/push/subscriptions").json()[0]

    assert len(stored["user_agent"]) == 300


if __name__ == "__main__":
    pytest_bazel.main()
