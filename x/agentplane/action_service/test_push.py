"""Real PostgreSQL subscription locks and restart recovery, with push transport recorded."""

import asyncio
from typing import cast

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.db import ActionStore, PushSubscriptionRow, make_sessionmaker
from x.agentplane.action_service.models import ActionRequestInput, DecisionInput, Principal, PrincipalRole, Verdict
from x.agentplane.action_service.push import (
    ActionPushNotifier,
    PushIdentity,
    PushRetract,
    PushShow,
    PushSubscriptionStore,
)

OPERATOR = Principal(issuer="test", subject="operator", role=PrincipalRole.OPERATOR)
CALLER = Principal(issuer="test", subject="caller", role=PrincipalRole.CALLER)


class RecordingNotifier(ActionPushNotifier):
    def __init__(
        self, engine: AsyncEngine, db_url: str, recorded: list[tuple[str, str]], *, fail: bool = False
    ) -> None:
        super().__init__(
            cast(PushIdentity, None),
            PushSubscriptionStore(make_sessionmaker(engine)),
            base_url="https://app.example",
            database_url=db_url,
            authorized_operators=frozenset({OPERATOR.key}),
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
        await subscriptions.save(
            operator_principal=OPERATOR.key, endpoint=endpoint, p256dh="test", auth="test", user_agent="test"
        )
    store = ActionStore(sessions)
    request, _ = await store.submit(
        ActionRequestInput(action=ActionIdentity(group="test", name="echo"), arguments={}, idempotency_key="push"),
        CALLER,
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


async def test_registration_owner_cannot_be_overwritten(engine: AsyncEngine) -> None:
    store = PushSubscriptionStore(make_sessionmaker(engine))
    args = {"endpoint": "https://push.example/a", "p256dh": "test", "auth": "test", "user_agent": "test"}
    await store.save(operator_principal=OPERATOR.key, **args)
    with pytest.raises(ValueError, match="another operator"):
        await store.save(operator_principal="other", **args)
    assert not await store.delete(operator_principal="other", endpoint=args["endpoint"])
    assert len(await store.list_for(OPERATOR.key)) == 1
    assert await store.delete(operator_principal=OPERATOR.key, endpoint=args["endpoint"])
    assert not await store.list_for(OPERATOR.key)


if __name__ == "__main__":
    pytest_bazel.main()
