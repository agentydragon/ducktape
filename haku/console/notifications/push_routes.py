"""Operator-browser API for managing this console's Web Push subscriptions.

The trust story is short: these routes only let an authenticated Operator tell the console where
to *reach* their own browsers. Nothing here grants authority over a tool call — a delivered push
is a prompt, and the decision it leads to goes through the ordinary
`POST /api/tool-calls/{id}/decision` under the same Authentik session as a click in the console.
Delivery itself lives in `push.py`.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from haku.console.notifications.push import PostgresPushSubscriptionStore, PushIdentity
from haku.console.operator_auth import OperatorActorDep

router = APIRouter(tags=["push"])

# A user agent string is operator-facing labelling for the Settings device list, not a key. Cap it
# so a hostile or broken client cannot write unbounded text into the console's database.
_MAX_USER_AGENT_LENGTH = 300


class PushConfigResponse(BaseModel):
    application_server_key: str | None = Field(
        description="VAPID public key for PushManager.subscribe, or null when push is not configured."
    )


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    p256dh: str = Field(description="The subscription's own P-256 public key (base64url).")
    auth: str = Field(description="The subscription's own RFC 8291 auth secret (base64url).")


class PushSubscriptionResponse(BaseModel):
    endpoint: str
    user_agent: str | None
    created_at: str


def _identity(request: Request) -> PushIdentity | None:
    return cast(PushIdentity | None, request.app.state.push_identity)


def _store(request: Request) -> PostgresPushSubscriptionStore:
    return cast(PostgresPushSubscriptionStore, request.app.state.push_subscription_store)


PushIdentityDep = Annotated[PushIdentity | None, Depends(_identity)]
PushSubscriptionStoreDep = Annotated[PostgresPushSubscriptionStore, Depends(_store)]


def _require_identity(identity: PushIdentity | None) -> PushIdentity:
    if identity is None:
        raise HTTPException(status_code=503, detail="web push is not configured on this console")
    return identity


@router.get("/api/push/config")
async def push_config(identity: PushIdentityDep) -> PushConfigResponse:
    """The key the SPA needs to subscribe — null rather than 503, so the UI can say push is off."""
    return PushConfigResponse(application_server_key=identity.application_server_key if identity else None)


@router.post("/api/push/subscriptions", status_code=204)
async def subscribe(
    body: PushSubscriptionRequest,
    actor: OperatorActorDep,
    store: PushSubscriptionStoreDep,
    identity: PushIdentityDep,
    user_agent: Annotated[str | None, Header()] = None,
) -> None:
    _require_identity(identity)
    await store.save(
        operator_id=actor.operator_id,
        endpoint=body.endpoint,
        p256dh=body.p256dh,
        auth=body.auth,
        user_agent=user_agent[:_MAX_USER_AGENT_LENGTH] if user_agent else None,
    )


@router.get("/api/push/subscriptions")
async def list_subscriptions(
    actor: OperatorActorDep, store: PushSubscriptionStoreDep
) -> list[PushSubscriptionResponse]:
    return [
        PushSubscriptionResponse(
            endpoint=subscription.endpoint,
            user_agent=subscription.user_agent,
            created_at=subscription.created_at.isoformat(),
        )
        for subscription in await store.list_for(actor.operator_id)
    ]


@router.delete("/api/push/subscriptions", status_code=204)
async def unsubscribe(endpoint: str, actor: OperatorActorDep, store: PushSubscriptionStoreDep) -> None:
    """Forget one device. Idempotent: a browser that already dropped its subscription locally
    still gets a clean unsubscribe, and an endpoint this Operator never owned is not found."""
    if not await store.delete(operator_id=actor.operator_id, endpoint=endpoint):
        raise HTTPException(status_code=404, detail="no such push subscription for this operator")
