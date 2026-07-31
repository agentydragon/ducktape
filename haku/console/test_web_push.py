"""Web Push delivery: VAPID identity, payload fan-out, and subscription pruning."""

from __future__ import annotations

import base64
import datetime
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from http_ece import decrypt

from haku.console.conftest import TEST_WEB_PUSH, console_sessions, operator_identity_store
from haku.console.tool_calls import AgentToolCallCaller, ToolCallRecord, ToolCallStatus
from haku.console.web_push import PostgresPushSubscriptionStore, WebPushApprovalNotifier, WebPushIdentity


def _record(tool_call_id: str = "tc_0123456789abcdef01234567", **overrides: Any) -> ToolCallRecord:
    now = datetime.datetime.now(datetime.UTC)
    return ToolCallRecord(
        **{
            "tool_call_id": tool_call_id,
            "server_id": "gmail",
            "tool_name": "drafts_create",
            "caller": AgentToolCallCaller(agent_id=UUID(int=1), display_name="Haku"),
            "status": ToolCallStatus.PENDING_APPROVAL,
            "created_at": now,
            "updated_at": now,
            "arguments": {"to": "someone@example.com"},
            "rationale": "Reply to the thread about the invoice.",
            **overrides,
        }
    )


class _Subscriber:
    """A browser subscription: generates its own keys and decrypts what the console sends it.

    Decrypting for real is the point — it proves the console encrypts to the subscription's keys
    per RFC 8291 rather than merely POSTing a plausible-looking body somewhere.
    """

    def __init__(self) -> None:
        self._private_key = ec.generate_private_key(ec.SECP256R1())
        self.auth_secret = b"0123456789abcdef"
        self.p256dh = base64.urlsafe_b64encode(
            self._private_key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        ).rstrip(b"=")
        self.auth = base64.urlsafe_b64encode(self.auth_secret).rstrip(b"=")

    def decrypt(self, body: bytes) -> dict[str, Any]:
        plaintext = decrypt(body, private_key=self._private_key, auth_secret=self.auth_secret, version="aes128gcm")
        decoded: dict[str, Any] = json.loads(plaintext)
        return decoded


@pytest.fixture
def subscriptions(migrated_db_url: str) -> PostgresPushSubscriptionStore:
    return PostgresPushSubscriptionStore(console_sessions(migrated_db_url))


@pytest.fixture
def operator_id(migrated_db_url: str) -> UUID:
    return operator_identity_store(migrated_db_url).resolve_configured_external_user_key("push-operator")


def _notifier(subscriptions: PostgresPushSubscriptionStore, handler: httpx.MockTransport) -> WebPushApprovalNotifier:
    return WebPushApprovalNotifier(
        identity=WebPushIdentity(TEST_WEB_PUSH),
        subscriptions=subscriptions,
        console_base_url="https://haku.test",
        client=httpx.AsyncClient(transport=handler),
    )


def test_application_server_key_is_the_uncompressed_point_browsers_expect() -> None:
    key = WebPushIdentity(TEST_WEB_PUSH).application_server_key

    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    # 0x04 || X || Y — what PushManager.subscribe takes as applicationServerKey.
    assert len(raw) == 65
    assert raw[0] == 0x04
    assert "=" not in key


def test_vapid_authorization_is_audienced_to_the_push_service_origin() -> None:
    identity = WebPushIdentity(TEST_WEB_PUSH)

    header = identity.authorization("https://fcm.googleapis.com/fcm/send/abc123?x=1")["Authorization"]

    assert header.startswith("vapid t=")
    token, key = header.removeprefix("vapid t=").split(",k=")
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    # The audience is the push service's origin only — never the endpoint path, which is the
    # capability to push to that one browser.
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "mailto:operator@example.com"
    assert key == identity.application_server_key


async def test_pending_push_is_encrypted_to_the_subscription_and_carries_the_deep_link(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID
) -> None:
    subscriber = _Subscriber()
    subscriptions.save(
        operator_id=operator_id,
        endpoint="https://push.test/endpoint/a",
        p256dh=subscriber.p256dh.decode(),
        auth=subscriber.auth.decode(),
        user_agent="Firefox",
    )
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201)

    record = _record()
    await _notifier(subscriptions, httpx.MockTransport(handle)).tool_call_pending(
        operator_id=operator_id, record=record
    )

    assert len(sent) == 1
    assert sent[0].headers["content-encoding"] == "aes128gcm"
    assert sent[0].headers["authorization"].startswith("vapid t=")
    # Collapse key: a later retraction for the same call supersedes this message while queued.
    assert sent[0].headers["topic"] == record.tool_call_id
    # The call's identity and arguments travel raw: the service worker describes it through the
    # same registry the approvals card uses, so the two surfaces cannot phrase it differently.
    assert subscriber.decrypt(sent[0].content) == {
        "kind": "show",
        "tool_call_id": record.tool_call_id,
        "server_id": "gmail",
        "tool_name": "drafts_create",
        "arguments": {"to": "someone@example.com"},
        "rationale": "Reply to the thread about the invoice.",
        # A console-owned path under the reserved /_console namespace — not /tool-calls/<id>,
        # which the shell mirrors into the haku-ui frame.
        "url": f"https://haku.test/_console/tool-calls/{record.tool_call_id}",
    }


async def test_retraction_names_the_outcome_the_call_actually_reached(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID
) -> None:
    subscriber = _Subscriber()
    subscriptions.save(
        operator_id=operator_id,
        endpoint="https://push.test/endpoint/a",
        p256dh=subscriber.p256dh.decode(),
        auth=subscriber.auth.decode(),
        user_agent=None,
    )
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201)

    notifier = _notifier(subscriptions, httpx.MockTransport(handle))

    for status, expected in [
        (ToolCallStatus.DENIED, "Denied"),
        (ToolCallStatus.WITHDRAWN, "Withdrawn by the requester"),
        (ToolCallStatus.RUNNING, "Approved"),
    ]:
        await notifier.tool_call_resolved(operator_id=operator_id, record=_record(status=status))
        assert subscriber.decrypt(sent[-1].content)["outcome"] == expected


async def test_every_device_the_operator_enrolled_is_notified_and_retracted(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID
) -> None:
    """One operator, many browsers — the point of the feature.

    Retraction has to fan out as widely as the notification did, or approving on the laptop
    leaves the phone still offering buttons for a call that is already running.
    """
    devices = {}
    for endpoint in ("https://push.test/laptop", "https://push.test/phone", "https://push.test/tablet"):
        subscriber = _Subscriber()
        devices[endpoint] = subscriber
        subscriptions.save(
            operator_id=operator_id,
            endpoint=endpoint,
            p256dh=subscriber.p256dh.decode(),
            auth=subscriber.auth.decode(),
            user_agent=None,
        )
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201)

    notifier = _notifier(subscriptions, httpx.MockTransport(handle))
    record = _record()
    await notifier.tool_call_pending(operator_id=operator_id, record=record)
    await notifier.tool_call_resolved(operator_id=operator_id, record=_record(status=ToolCallStatus.RUNNING))

    assert len(sent) == 6
    # Each device gets both edges, and each is encrypted to that device's own keys — a payload
    # is never readable by a subscription other than the one it was addressed to.
    for endpoint, subscriber in devices.items():
        addressed = [request for request in sent if str(request.url) == endpoint]
        assert [subscriber.decrypt(request.content)["kind"] for request in addressed] == ["show", "retract"]


async def test_one_operators_push_never_reaches_anothers_device(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID, migrated_db_url: str
) -> None:
    other = operator_identity_store(migrated_db_url).resolve_configured_external_user_key("other-operator")
    for owner, endpoint in [(operator_id, "https://push.test/mine"), (other, "https://push.test/theirs")]:
        subscriber = _Subscriber()
        subscriptions.save(
            operator_id=owner,
            endpoint=endpoint,
            p256dh=subscriber.p256dh.decode(),
            auth=subscriber.auth.decode(),
            user_agent=None,
        )
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201)

    await _notifier(subscriptions, httpx.MockTransport(handle)).tool_call_pending(
        operator_id=operator_id, record=_record()
    )

    assert [str(request.url) for request in sent] == ["https://push.test/mine"]


async def test_a_gone_subscription_is_pruned_and_a_transient_failure_is_not(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID
) -> None:
    """404/410 means this browser is unreachable forever; anything else is weather.

    Deleting on a 500 would quietly unsubscribe the operator's phone during a push-service
    outage, and they would only discover it by not being notified.
    """
    for endpoint in ("https://push.test/gone", "https://push.test/flaky"):
        subscriber = _Subscriber()
        subscriptions.save(
            operator_id=operator_id,
            endpoint=endpoint,
            p256dh=subscriber.p256dh.decode(),
            auth=subscriber.auth.decode(),
            user_agent=None,
        )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410 if "gone" in str(request.url) else 503)

    await _notifier(subscriptions, httpx.MockTransport(handle)).tool_call_pending(
        operator_id=operator_id, record=_record()
    )

    remaining = subscriptions.list_for(operator_id)
    assert [subscription.endpoint for subscription in remaining] == ["https://push.test/flaky"]
    assert remaining[0].last_failure_at is not None


async def test_an_operator_with_no_subscriptions_sends_nothing(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no push should be attempted")

    await _notifier(subscriptions, httpx.MockTransport(handle)).tool_call_pending(
        operator_id=operator_id, record=_record()
    )


def test_resubscribing_the_same_endpoint_replaces_rather_than_duplicates(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID
) -> None:
    """Browsers re-present a subscription on their own schedule; each must not add a row."""
    for user_agent in ("Firefox", "Firefox 2"):
        subscriptions.save(
            operator_id=operator_id,
            endpoint="https://push.test/endpoint/a",
            p256dh="key",
            auth="auth",
            user_agent=user_agent,
        )

    stored = subscriptions.list_for(operator_id)
    assert len(stored) == 1
    assert stored[0].user_agent == "Firefox 2"


def test_delete_is_scoped_to_the_owning_operator(
    subscriptions: PostgresPushSubscriptionStore, operator_id: UUID, migrated_db_url: str
) -> None:
    other = operator_identity_store(migrated_db_url).resolve_configured_external_user_key("other-operator")
    subscriptions.save(operator_id=operator_id, endpoint="https://push.test/a", p256dh="k", auth="a", user_agent=None)

    assert subscriptions.delete(operator_id=other, endpoint="https://push.test/a") is False
    assert subscriptions.delete(operator_id=operator_id, endpoint="https://push.test/a") is True


if __name__ == "__main__":
    pytest_bazel.main()
