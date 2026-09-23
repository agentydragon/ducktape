"""Real PostgreSQL subscription locks and restart recovery, with push transport recorded."""

import asyncio
from typing import cast
from uuid import uuid4

import pytest
import pytest_bazel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.db import ActionStore, PushSubscriptionRow, make_sessionmaker
from agentplane.action_service.models import (
    ActionRequestInput,
    CallerPrincipal,
    DecisionInput,
    OperatorPrincipal,
    ProviderOutcome,
    ProviderVerdict,
    ProviderVote,
    Verdict,
)
from agentplane.action_service.push import (
    ActionPushNotifier,
    PushIdentity,
    PushRetract,
    PushShow,
    PushSubscriptionStore,
    WebPushSettings,
)
from agentplane.subjects import ServiceAccountRef

OPERATOR = OperatorPrincipal(issuer="test", subject="operator")
OTHER_OPERATOR = OperatorPrincipal(issuer="test", subject="other-operator")
CALLER = CallerPrincipal(account=ServiceAccountRef(namespace="agentplane-test", name="test-caller"))


class RecordingNotifier(ActionPushNotifier):
    def __init__(
        self, engine: AsyncEngine, db_url: str, recorded: list[tuple[str, str]], *, fail: bool = False
    ) -> None:
        super().__init__(
            cast(PushIdentity, None),
            PushSubscriptionStore(make_sessionmaker(engine)),
            base_url="https://app.example",
            database_url=db_url,
        )
        self.recorded = recorded
        self.fail = fail

    async def _send_one(self, row: PushSubscriptionRow, message: PushShow | PushRetract) -> str:
        if self.fail:
            return "retry"
        self.recorded.append((row.endpoint, message.kind))
        return "sent"


async def test_replica_delivery_and_recovery(engine: AsyncEngine, db_url: str) -> None:
    sessions = make_sessionmaker(engine)
    subscriptions = PushSubscriptionStore(sessions)
    for endpoint in ("https://push.example/a", "https://push.example/b"):
        await subscriptions.save(operator=OPERATOR, endpoint=endpoint, p256dh="test", auth="test", user_agent="test")
    store = ActionStore(sessions)
    request = await store.submit(
        ActionRequestInput(
            action=ActionIdentity(group="test", name="echo"),
            arguments={},
            idempotency_key="push",
            title="test title for push",
        ),
        CALLER,
        request_id=uuid4(),
        vote=None,
    )
    recorded: list[tuple[str, str]] = []
    first = RecordingNotifier(engine, db_url, recorded, fail=True)
    second = RecordingNotifier(engine, db_url, recorded)
    try:
        assert not await first.reconcile()
        assert not recorded
        first.fail = False
        await asyncio.gather(first.reconcile(), second.reconcile())
        assert sorted(recorded) == [("https://push.example/a", "show"), ("https://push.example/b", "show")]
        await store.decide(
            request.id,
            DecisionInput(verdict=Verdict.DENY, expected_version=1, idempotency_key="deny"),
            OPERATOR,
            provider="human_operator",
        )
        # A replacement worker recovers from durable state without receiving the original wake.
        await second.reconcile()
        assert sorted(recorded) == [
            ("https://push.example/a", "retract"),
            ("https://push.example/a", "show"),
            ("https://push.example/b", "retract"),
            ("https://push.example/b", "show"),
        ]
        assert not await first.reconcile()
    finally:
        await first.close()
        await second.close()


async def test_auto_approved_request_is_never_pushed(engine: AsyncEngine, db_url: str) -> None:
    sessions = make_sessionmaker(engine)
    await PushSubscriptionStore(sessions).save(
        operator=OPERATOR, endpoint="https://push.example/a", p256dh="test", auth="test", user_agent="test"
    )
    await ActionStore(sessions).submit(
        ActionRequestInput(
            action=ActionIdentity(group="test", name="echo"),
            arguments={},
            idempotency_key="auto-approved",
            title="test title for auto-approved",
        ),
        CALLER,
        request_id=uuid4(),
        vote=ProviderVote(
            provider="test-policy", outcome=ProviderOutcome(verdict=ProviderVerdict.ALLOW, reason_code="test-allow")
        ),
    )
    recorded: list[tuple[str, str]] = []
    notifier = RecordingNotifier(engine, db_url, recorded)
    try:
        assert not await notifier.reconcile()
        assert not recorded
    finally:
        await notifier.close()


async def test_registration_owner_cannot_be_overwritten(engine: AsyncEngine) -> None:
    store = PushSubscriptionStore(make_sessionmaker(engine))
    args = {"endpoint": "https://push.example/a", "p256dh": "test", "auth": "test", "user_agent": "test"}
    await store.save(operator=OPERATOR, **args)
    with pytest.raises(ValueError, match="another operator"):
        await store.save(operator=OTHER_OPERATOR, **args)
    assert not await store.delete(operator=OTHER_OPERATOR, endpoint=args["endpoint"])
    assert len(await store.list_for(OPERATOR)) == 1
    assert await store.delete(operator=OPERATOR, endpoint=args["endpoint"])
    assert not await store.list_for(OPERATOR)


def test_push_identity_signs_only_for_reviewed_push_hosts() -> None:
    private_key = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    settings = WebPushSettings(
        private_key_pem=private_key,
        subject="mailto:push@agentplane.test",
        public_base_url="https://app.example",
        allowed_push_hosts=frozenset({"push.example"}),
    )
    identity = PushIdentity(settings)
    assert identity.application_server_key == PushIdentity(settings).application_server_key
    assert len(identity.application_server_key) == 87
    identity.validate_endpoint("https://push.example/test-subscription")
    assert identity.authorization("https://push.example/test-subscription").startswith("vapid ")
    for endpoint in ("https://unreviewed.example/push", "http://push.example/push", "https://push.example:8443/push"):
        with pytest.raises(ValueError, match="configured HTTPS push service"):
            identity.validate_endpoint(endpoint)


if __name__ == "__main__":
    pytest_bazel.main()
