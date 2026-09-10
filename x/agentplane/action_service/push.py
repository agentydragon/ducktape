"""Action approval Web Push delivery.

The Action Service owns delivery because it observes the committed Action event stream. Browser
registration and the service worker remain integration-app concerns; subscriptions are scoped to
the authenticated operator principal and never grant authority by themselves.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pydantic import BaseModel, Field, SecretStr
from pywebpush import WebPusher
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from x.agentplane.action_service.db import ActionRequestRow, PushDeliveryRow, PushSubscriptionRow
from x.agentplane.action_service.models import ActionState
from x.agentplane.action_service.updates import ActionUpdates

logger = logging.getLogger(__name__)
PUSH_TTL_SECONDS = 600
_DEAD_SUBSCRIPTION_STATUSES = {404, 410}


class WebPushSettings(BaseModel):
    private_key_pem: SecretStr = Field(min_length=1)
    subject: str = Field(min_length=1)
    public_base_url: str = Field(min_length=1)
    allowed_push_hosts: frozenset[str] = Field(
        min_length=1, description="Exact reviewed browser push-service hostnames; no arbitrary callback destinations."
    )


class PushShow(BaseModel):
    kind: str = "show"
    action_id: str
    action_group: str
    action_name: str
    version: int
    url: str


class PushRetract(BaseModel):
    kind: str = "retract"
    action_id: str
    outcome: str


class PushIdentity:
    def __init__(self, settings: WebPushSettings) -> None:
        self._vapid = Vapid02.from_pem(settings.private_key_pem.get_secret_value().encode())
        self._subject = settings.subject
        self._allowed_hosts = settings.allowed_push_hosts

    def validate_endpoint(self, endpoint: str) -> None:
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or url.hostname not in self._allowed_hosts
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.port not in (None, 443)
        ):
            raise ValueError("push endpoint is not a configured HTTPS push service")

    @property
    def application_server_key(self) -> str:
        point = self._vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(point).rstrip(b"=").decode()

    def authorization(self, endpoint: str) -> str:
        self.validate_endpoint(endpoint)
        parsed = httpx.URL(endpoint)
        expiry = int(datetime.datetime.now(datetime.UTC).timestamp()) + 3 * 60 * 60
        audience = f"{parsed.scheme}://{parsed.netloc.decode()}"
        return str(self._vapid.sign({"aud": audience, "sub": self._subject, "exp": expiry})["Authorization"])


class PushSubscriptionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(
        self, *, operator_principal: str, endpoint: str, p256dh: str, auth: str, user_agent: str | None
    ) -> None:
        statement = (
            insert(PushSubscriptionRow)
            .values(
                endpoint=endpoint,
                operator_principal=operator_principal,
                p256dh=p256dh,
                auth=auth,
                user_agent=user_agent,
                created_at=datetime.datetime.now(datetime.UTC),
            )
            .on_conflict_do_update(
                index_elements=[PushSubscriptionRow.endpoint],
                where=PushSubscriptionRow.operator_principal == operator_principal,
                set_={"p256dh": p256dh, "auth": auth, "user_agent": user_agent},
            )
        )
        async with self._sessions.begin() as session:
            saved = await session.scalar(statement.returning(PushSubscriptionRow.endpoint))
            if saved is None:
                raise ValueError("subscription belongs to another operator")

    async def list_for(self, operator_principal: str) -> list[PushSubscriptionRow]:
        async with self._sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(PushSubscriptionRow).where(PushSubscriptionRow.operator_principal == operator_principal)
                    )
                ).all()
            )

    async def list_all(self) -> list[PushSubscriptionRow]:
        async with self._sessions() as session:
            return list((await session.scalars(select(PushSubscriptionRow))).all())

    async def delete(self, *, operator_principal: str, endpoint: str) -> bool:
        async with self._sessions.begin() as session:
            row = await session.get(PushSubscriptionRow, endpoint)
            if row is None or row.operator_principal != operator_principal:
                return False
            await session.delete(row)
            return True

    async def drop_dead(self, endpoint: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(delete(PushSubscriptionRow).where(PushSubscriptionRow.endpoint == endpoint))


class ActionPushNotifier:
    """NOTIFY wakes durable reconciliation; browser-row locks serialize sends across replicas.

    Network sends are bounded and run under the subscription lock, never the Action lock.
    A crash after sending but before commit can resend: stable browser notification tags collapse
    that retry. A send receipt is not proof of browser delivery. No Action is executed here.
    """

    def __init__(
        self, identity: PushIdentity, subscriptions: PushSubscriptionStore, *, base_url: str, database_url: str
    ) -> None:
        self._identity = identity
        self._subscriptions = subscriptions
        self._base_url = base_url.rstrip("/")
        self._database_url = database_url
        self._http = httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="action-web-push")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._http.aclose()

    async def run(self) -> None:
        while True:
            updates = ActionUpdates(self._database_url)
            try:
                await updates.start()
                with updates.subscribe_all() as changed:
                    while True:
                        changed.clear()
                        updates.check_available()
                        if await self.reconcile():
                            continue
                        try:
                            async with asyncio.timeout(30):
                                await changed.wait()
                        except TimeoutError:
                            pass  # Background delivery retry/recovery only; the UI never polls.
            except Exception:
                logger.warning("push reconciliation unavailable; retrying without logging payloads")
            finally:
                await updates.close()
            await asyncio.sleep(5)

    async def reconcile(self) -> bool:
        delivered = False
        for registration in await self._subscriptions.list_all():
            # One short transaction per send: a slow device never locks Action state or all browsers.
            async with self._subscriptions._sessions.begin() as session:
                subscription = await session.scalar(
                    select(PushSubscriptionRow)
                    .where(PushSubscriptionRow.endpoint == registration.endpoint)
                    .with_for_update(skip_locked=True)
                )
                if subscription is None:
                    continue
                delivery = PushDeliveryRow
                request = await session.scalar(
                    select(ActionRequestRow)
                    .outerjoin(
                        delivery,
                        and_(delivery.request_id == ActionRequestRow.id, delivery.endpoint == subscription.endpoint),
                    )
                    .where(
                        or_(
                            and_(ActionRequestRow.state == ActionState.DECISION_PENDING, delivery.request_id.is_(None)),
                            and_(ActionRequestRow.state != ActionState.DECISION_PENDING, delivery.kind == "show"),
                        )
                    )
                    .order_by(ActionRequestRow.created_at)
                    .limit(1)
                )
                if request is None:
                    continue
                if request.state == ActionState.DECISION_PENDING:
                    message: PushShow | PushRetract = PushShow(
                        action_id=str(request.id),
                        action_group=request.action["group"],
                        action_name=request.action["name"],
                        version=request.version,
                        url=f"{self._base_url}/#/actions/{request.id}",
                    )
                else:
                    message = PushRetract(action_id=str(request.id), outcome=request.state.replace("_", " "))
                result = await self._send_one(subscription, message)
                if result == "dead":
                    delivered = True
                    await session.delete(subscription)
                elif result == "sent":
                    delivered = True
                    await session.execute(
                        insert(delivery)
                        .values(request_id=request.id, endpoint=subscription.endpoint, kind=message.kind)
                        .on_conflict_do_update(
                            index_elements=[delivery.request_id, delivery.endpoint], set_={"kind": message.kind}
                        )
                    )

        return delivered

    async def _send_one(self, row: PushSubscriptionRow, message: PushShow | PushRetract) -> str:
        try:
            self._identity.validate_endpoint(row.endpoint)
            encoded = WebPusher({"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}).encode(
                message.model_dump_json().encode()
            )
            async with asyncio.timeout(10):
                response = await self._http.post(
                    row.endpoint,
                    content=encoded["body"],
                    headers={
                        "Authorization": self._identity.authorization(row.endpoint),
                        "Content-Encoding": "aes128gcm",
                        "Content-Type": "application/octet-stream",
                        "TTL": str(PUSH_TTL_SECONDS),
                        "Urgency": "high",
                        "Topic": UUID(message.action_id).hex,
                    },
                )
            if response.status_code in _DEAD_SUBSCRIPTION_STATUSES:
                return "dead"
            if response.is_success:
                return "sent"
        except Exception:
            pass
        logger.warning("web push delivery failed; sensitive details withheld")
        return "retry"
