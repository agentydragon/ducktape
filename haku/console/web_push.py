"""Web Push delivery of pending-approval notifications to the Operator's own browsers.

The console's event socket only reaches a browser already running with the console loaded, which
is exactly not the case when the operator is away from the desk — the moment a pending call most
needs to reach them. Web Push covers that gap: the browser's push service holds the message and
wakes the console's service worker, which renders an OS notification carrying Approve/Deny.

**A push message is a prompt to decide, never the decision.** The notification is rendered by the
OS from console-authored content, its buttons are defined by console code, and acting on one is a
same-origin `fetch` from the console's own service worker carrying the operator's ordinary
Authentik session. No new authority exists, and someone who intercepted a push still could not
approve anything — `haku/docs/security.md` invariant #4, consent happens on trusted console
surfaces.

Two RFCs are in play, both handled by `pywebpush`/`py_vapid`: RFC 8292 (VAPID — the console
signs each push with the keypair the browser bound to the subscription) and RFC 8291 (payload
encryption to the subscription's own keys, so the push service relays ciphertext it cannot
read). This module keeps their *transport* on the console's async httpx rather than pywebpush's
`requests`, so a slow or wedged push endpoint cannot occupy a thread of the API service.

**The payload below is a compatibility boundary, not an internal type.** The console deploys
atomically; the code that *reads* these messages does not. A service worker updates only when the
browser decides to check — on a navigation to the console, or after handling a push once the
registration has gone stale (>24h) — so an installed worker can be a day or more behind this file.
Consequences for editing `PushShow`/`PushRetract`:

- **Adding a field is safe**; an old worker ignores what it does not read.
- **Renaming or removing one is not.** The old worker reads `undefined` and renders a broken
  notification with no error anywhere the operator sees.
- A change that cannot be additive needs a `kind` variant, so old workers fall through their
  existing branches rather than misreading a familiar one.

Mirrored in `frontend/sw.ts`, which declares the reading half.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
from typing import Any, Literal
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pydantic import BaseModel, Field
from pywebpush import WebPusher
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import WebPushConfig, tool_call_console_url
from haku.console.database_schema import PushSubscription
from haku.console.tool_calls import ToolCallRecord, ToolCallStatus

logger = logging.getLogger(__name__)


# How long a push service should hold an undelivered message. A pending approval is a live
# request someone is waiting on, not a bulletin: a phone that has been off for an hour should
# not light up with a decision that has long since been made or timed out elsewhere.
PUSH_TTL_SECONDS = 600
# VAPID tokens are accepted for at most 24h (RFC 8292 §2); keep them far shorter, since one is
# minted per send anyway.
_VAPID_TOKEN_LIFETIME_SECONDS = 3 * 60 * 60
_SEND_TIMEOUT_SECONDS = 10.0
# A push service reports a subscription that will never work again as Gone/Not Found. Anything
# else (throttling, 5xx, a network blip) is transient and must not delete the operator's device.
_DEAD_SUBSCRIPTION_STATUSES = {404, 410}
_BODY_MAX_CHARS = 240


class PushShow(BaseModel):
    """Render (or replace) the notification for one pending tool call.

    The call's identity and arguments travel raw rather than pre-rendered: the service worker
    describes it through the same registry the approvals card uses
    (`frontend/tool_rendering/actions.ts`), so a notification reads "Gmail: Draft email" instead
    of "gmail · drafts_create" and the two surfaces cannot drift into different phrasings for the
    same call. The payload is encrypted to the subscription, so this exposes nothing the operator's
    own browser would not already show.

    **This is a versioned wire contract — see the module docstring.** Add fields freely; renaming
    or removing one breaks every browser still running an older service worker.
    """

    kind: Literal["show"] = "show"
    tool_call_id: str
    server_id: str
    tool_name: str
    arguments: dict[str, Any]
    rationale: str
    url: str = Field(description="Console deep link the notification opens when tapped.")


class PushRetract(BaseModel):
    """The call left the queue; collapse its notification to a resolved, non-actionable one.

    Not a bare "close": Chrome requires a `userVisibleOnly` subscription to show *something* per
    push, and a handler that silently shows nothing spends push budget until the browser starts
    substituting its own "site updated in the background" notice. Replacing in place is also the
    better UX — the operator sees that the thing they were pinged about is settled, and by what.
    """

    kind: Literal["retract"] = "retract"
    tool_call_id: str
    outcome: str


type PushMessage = PushShow | PushRetract


class NullApprovalNotifier:
    """The `PendingApprovalNotifier` used when no VAPID identity is configured."""

    async def tool_call_pending(self, *, operator_id: UUID, record: ToolCallRecord) -> None:
        return None

    async def tool_call_resolved(self, *, operator_id: UUID, record: ToolCallRecord) -> None:
        return None

    async def aclose(self) -> None:
        return None


class WebPushIdentity:
    """This console's VAPID keypair, and the per-request authorization it signs."""

    def __init__(self, config: WebPushConfig) -> None:
        self._vapid = Vapid02.from_pem(config.private_key_pem.get_secret_value().encode())
        self._subject = config.subject

    @property
    def application_server_key(self) -> str:
        """The public key in the form `PushManager.subscribe` takes: base64url, unpadded."""
        point = self._vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(point).rstrip(b"=").decode()

    def authorization(self, endpoint: str) -> dict[str, str]:
        """Sign a VAPID token for one push service, whose origin is the token's audience."""
        parsed = httpx.URL(endpoint)
        expiry = int(datetime.datetime.now(tz=datetime.UTC).timestamp()) + _VAPID_TOKEN_LIFETIME_SECONDS
        audience = f"{parsed.scheme}://{parsed.netloc.decode()}"
        signed = self._vapid.sign({"aud": audience, "sub": self._subject, "exp": expiry})
        return {"Authorization": str(signed["Authorization"])}


class PostgresPushSubscriptionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, *, operator_id: UUID, endpoint: str, p256dh: str, auth: str, user_agent: str | None) -> None:
        """Record (or refresh) one device's subscription.

        Upsert rather than insert: a browser re-subscribes on its own schedule — after a
        permission re-grant, a key rotation, or an endpoint refresh it initiated — and each of
        those re-presents a subscription the console may already hold.
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        statement = (
            insert(PushSubscription)
            .values(
                endpoint=endpoint,
                operator_id=operator_id,
                p256dh=p256dh,
                auth=auth,
                user_agent=user_agent,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=[PushSubscription.endpoint],
                set_={
                    "operator_id": operator_id,
                    "p256dh": p256dh,
                    "auth": auth,
                    "user_agent": user_agent,
                    "last_failure_at": None,
                },
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def list_for(self, operator_id: UUID) -> list[PushSubscription]:
        async with self._sessions() as session:
            return list(
                (
                    await session.scalars(select(PushSubscription).where(PushSubscription.operator_id == operator_id))
                ).all()
            )

    async def delete(self, *, operator_id: UUID, endpoint: str) -> bool:
        """Drop one subscription. Scoped to its owner so an endpoint alone cannot unsubscribe it."""
        async with self._sessions.begin() as session:
            subscription = await session.get(PushSubscription, endpoint)
            if subscription is None or subscription.operator_id != operator_id:
                return False
            await session.delete(subscription)
            return True

    async def drop_dead(self, endpoint: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))

    async def record_failure(self, endpoint: str) -> None:
        async with self._sessions.begin() as session:
            subscription = await session.get(PushSubscription, endpoint)
            if subscription is not None:
                subscription.last_failure_at = datetime.datetime.now(tz=datetime.UTC)


def _notification_body(record: ToolCallRecord) -> str:
    """The rationale, trimmed to what a notification shade will actually show."""
    body = record.rationale.strip() or record.title or "Waiting for your approval."
    return body if len(body) <= _BODY_MAX_CHARS else f"{body[: _BODY_MAX_CHARS - 1]}…"


def _outcome(record: ToolCallRecord) -> str:
    match record.status:
        case ToolCallStatus.DENIED:
            return "Denied"
        case ToolCallStatus.WITHDRAWN:
            return "Withdrawn by the requester"
        case _:
            # Everything else reaches here having been approved: RUNNING right after the decision,
            # or already OK/ERROR if execution beat the notification out the door.
            return "Approved"


class WebPushApprovalNotifier:
    """Fans one tool-call transition out to every browser the Operator has subscribed."""

    def __init__(
        self,
        *,
        identity: WebPushIdentity,
        subscriptions: PostgresPushSubscriptionStore,
        console_base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._identity = identity
        self._subscriptions = subscriptions
        self._console_base_url = console_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def tool_call_pending(self, *, operator_id: UUID, record: ToolCallRecord) -> None:
        await self._send(
            operator_id,
            PushShow(
                tool_call_id=record.tool_call_id,
                server_id=record.server_id,
                tool_name=record.tool_name,
                arguments=record.arguments,
                rationale=_notification_body(record),
                url=tool_call_console_url(self._console_base_url, record.tool_call_id),
            ),
        )

    async def tool_call_resolved(self, *, operator_id: UUID, record: ToolCallRecord) -> None:
        await self._send(operator_id, PushRetract(tool_call_id=record.tool_call_id, outcome=_outcome(record)))

    async def _send(self, operator_id: UUID, message: PushMessage) -> None:
        subscriptions = await self._subscriptions.list_for(operator_id)
        if not subscriptions:
            return
        payload = message.model_dump_json().encode()
        await asyncio.gather(
            *(self._send_one(subscription, payload, message.tool_call_id) for subscription in subscriptions)
        )

    async def _send_one(self, subscription: PushSubscription, payload: bytes, tool_call_id: str) -> None:
        try:
            encoded = WebPusher(
                {"endpoint": subscription.endpoint, "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}}
            ).encode(payload)
            headers = {
                **self._identity.authorization(subscription.endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(PUSH_TTL_SECONDS),
                "Urgency": "high",
                # Collapse key: a retraction supersedes the pending message for the same call
                # while it is still queued, so a phone that was offline through the whole
                # exchange wakes to the outcome instead of a dead ask it can no longer act on.
                "Topic": _collapse_topic(tool_call_id),
            }
            response = await self._client.post(subscription.endpoint, content=encoded["body"], headers=headers)
        except Exception:
            # Best-effort by construction: the ledger row is authoritative and the console UI is
            # already correct. A push that cannot be delivered must never fail the mutation that
            # produced it, but it is also not nothing — a silently dead channel is how an
            # operator stops trusting the notifications.
            logger.warning("web push send failed for %s", _redacted(subscription.endpoint), exc_info=True)
            await self._subscriptions.record_failure(subscription.endpoint)
            return
        if response.status_code in _DEAD_SUBSCRIPTION_STATUSES:
            logger.info("pruning expired push subscription %s", _redacted(subscription.endpoint))
            await self._subscriptions.drop_dead(subscription.endpoint)
            return
        if response.is_error:
            logger.warning(
                "web push rejected by %s: %s %s",
                _redacted(subscription.endpoint),
                response.status_code,
                response.text[:200],
            )
            await self._subscriptions.record_failure(subscription.endpoint)


def _collapse_topic(tool_call_id: str) -> str:
    """RFC 8030 §5.4 topics are base64url and at most 32 characters.

    Tool call ids (``tc_`` + 24 hex) already satisfy both, but the cap is the push service's, not
    ours — truncating keeps a future id format from turning every push into a 400.
    """
    return tool_call_id[:32]


def _redacted(endpoint: str) -> str:
    """A push endpoint's path is the capability to push to that browser; log only its origin."""
    parsed = httpx.URL(endpoint)
    return f"{parsed.scheme}://{parsed.netloc.decode()}/…"
