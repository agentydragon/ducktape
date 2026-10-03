"""Short fenced transactions; no network calls while holding the inbox prefix lock."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.action_service.models import ActionEventView
from agentplane.notification_service.db import Entry, Inbox, Match, Notice, Subscription
from agentplane.notification_service.models import (
    EntryView,
    InboxPage,
    InboxView,
    NoticeView,
    Subscribe,
    SubscriptionUpdate,
    SubscriptionView,
)
from agentplane.protocol import event_log_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipal

# gazelle:include_dep @pypi//protobuf


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class QuotaError(Exception):
    pass


class ClaimLostError(Exception):
    pass


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def owned(
        self, session: AsyncSession, inbox_id: UUID, owner: ServiceAccountRef, *, lock: bool = False
    ) -> Inbox:
        query = select(Inbox).where(
            Inbox.id == inbox_id, Inbox.owner_namespace == owner.namespace, Inbox.owner_name == owner.name
        )
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query)
        if row is None:
            raise NotFoundError
        return row

    async def subscribe(self, principal: WorkloadPrincipal, body: Subscribe) -> SubscriptionView:
        owner = principal.account
        now = datetime.now(UTC)
        key = hashlib.sha256(
            json.dumps([body.destination_ref.model_dump(), body.session_id], sort_keys=True).encode()
        ).hexdigest()
        async with self.sessions.begin() as session:
            # Serialize only creation for an owner, to enforce the total destination quota.
            await session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(f"{owner.namespace}/{owner.name}", 0)))
            )
            existing_id = await session.scalar(
                select(Inbox.id).where(
                    Inbox.owner_namespace == owner.namespace,
                    Inbox.owner_name == owner.name,
                    Inbox.destination_key == key,
                )
            )
            if existing_id is None:
                count = await session.scalar(
                    select(func.count())
                    .select_from(Inbox)
                    .where(Inbox.owner_namespace == owner.namespace, Inbox.owner_name == owner.name)
                )
                if count is not None and count >= 64:
                    raise QuotaError("64 inboxes per owning ServiceAccount")
            await session.execute(
                insert(Inbox)
                .values(
                    id=uuid4(),
                    owner_namespace=owner.namespace,
                    owner_name=owner.name,
                    destination_key=key,
                    destination_ref=body.destination_ref.model_dump(),
                    session_id=body.session_id,
                    last_cursor=0,
                    acknowledged=0,
                    covered=0,
                    expired_through=0,
                    retired=False,
                    updated_at=now,
                    next_poll=now,
                    claim_until=now,
                )
                .on_conflict_do_nothing()
            )
            inbox = await session.scalar(
                select(Inbox)
                .where(
                    Inbox.owner_namespace == owner.namespace,
                    Inbox.owner_name == owner.name,
                    Inbox.destination_key == key,
                )
                .with_for_update()
            )
            assert inbox is not None
            if inbox.retired:
                raise ConflictError("inbox is retired")
            row = await session.scalar(
                select(Subscription).where(
                    Subscription.inbox_id == inbox.id, Subscription.client_key == body.client_key
                )
            )
            if row is not None:
                if row.creation != body.model_dump(mode="json"):
                    raise ConflictError("creation key already names another subscription")
                return SubscriptionView.model_validate(row)
            count = await session.scalar(
                select(func.count()).select_from(Subscription).where(Subscription.inbox_id == inbox.id)
            )
            if count is not None and count >= 64:
                raise QuotaError("64 subscriptions per inbox, including cancelled subscriptions")
            row = Subscription(
                id=uuid4(),
                inbox_id=inbox.id,
                request_id=body.request_id,
                client_key=body.client_key,
                creation=body.model_dump(mode="json"),
                creator=asdict(principal),
                version=1,
                after_sequence=body.after_sequence,
                paused=False,
                cancelled=False,
                expires_at=now + timedelta(days=body.lifetime_days),
                next_poll=now,
            )
            session.add(row)
            inbox.updated_at = now
            await session.flush()
            return SubscriptionView.model_validate(row)

    async def subscriptions(self, owner: ServiceAccountRef, after_id: UUID | None = None) -> list[SubscriptionView]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(Subscription)
                .join(Inbox)
                .where(
                    Inbox.owner_namespace == owner.namespace,
                    Inbox.owner_name == owner.name,
                    Subscription.id > (after_id or UUID(int=0)),
                )
                .order_by(Subscription.id)
                .limit(128)
            )
            return [SubscriptionView.model_validate(row) for row in rows]

    async def subscription(self, owner: ServiceAccountRef, subscription_id: UUID) -> SubscriptionView:
        async with self.sessions() as session:
            row = await session.scalar(
                select(Subscription)
                .join(Inbox)
                .where(
                    Subscription.id == subscription_id,
                    Inbox.owner_namespace == owner.namespace,
                    Inbox.owner_name == owner.name,
                )
            )
            if row is None:
                raise NotFoundError
            return SubscriptionView.model_validate(row)

    async def change(
        self, owner: ServiceAccountRef, subscription_id: UUID, update: SubscriptionUpdate | None
    ) -> SubscriptionView:
        async with self.sessions.begin() as session:
            row = await session.get(Subscription, subscription_id)
            if row is None:
                raise NotFoundError
            inbox = await self.owned(session, row.inbox_id, owner, lock=True)
            if inbox.retired:
                raise ConflictError("inbox is retired")
            await session.refresh(row)
            if update is None:
                if row.cancelled:
                    return SubscriptionView.model_validate(row)
                row.cancelled = True
            else:
                if row.version != update.version or row.cancelled:
                    raise ConflictError("subscription version changed or subscription cancelled")
                row.paused = update.paused
                row.expires_at = datetime.now(UTC) + timedelta(days=update.lifetime_days)
            row.version += 1
            return SubscriptionView.model_validate(row)

    async def inboxes(self, owner: ServiceAccountRef) -> list[InboxView]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(Inbox)
                .where(Inbox.owner_namespace == owner.namespace, Inbox.owner_name == owner.name)
                .order_by(Inbox.id)
                .limit(1000)
            )
            return [InboxView.model_validate(row) for row in rows]

    async def read(self, owner: ServiceAccountRef, inbox_id: UUID, after: int, limit: int) -> InboxPage:
        async with self.sessions.begin() as session:
            # A short read lock makes entries and prefix metadata a consistent snapshot.
            inbox = await self.owned(session, inbox_id, owner, lock=True)
            rows = await session.scalars(
                select(Entry)
                .where(Entry.inbox_id == inbox_id, Entry.cursor > max(after, inbox.expired_through))
                .order_by(Entry.cursor)
                .limit(limit)
            )
            entries = []
            for row in rows:
                assert row.payload is not None
                matches = await session.scalars(
                    select(Match.subscription_id)
                    .join(Subscription)
                    .where(Subscription.inbox_id == inbox_id, Match.cursor == row.cursor)
                )
                entries.append(
                    EntryView(
                        cursor=row.cursor,
                        request_id=row.request_id,
                        source_sequence=row.source_sequence,
                        payload=row.payload,
                        subscriptions=list(matches),
                    )
                )
            notice = await session.get(Notice, inbox_id)
            return InboxPage(
                inbox=InboxView.model_validate(inbox),
                entries=entries,
                notice=NoticeView.model_validate(notice) if notice else None,
            )

    async def acknowledge(self, owner: ServiceAccountRef, inbox_id: UUID, through: int) -> InboxView:
        async with self.sessions.begin() as session:
            inbox = await self.owned(session, inbox_id, owner, lock=True)
            if through > inbox.last_cursor:
                raise ConflictError("cursor is beyond the committed prefix")
            inbox.acknowledged = max(inbox.acknowledged, through)
            inbox.updated_at = datetime.now(UTC)
            return InboxView.model_validate(inbox)

    async def retire(self, owner: ServiceAccountRef, inbox_id: UUID) -> None:
        async with self.sessions.begin() as session:
            inbox = await self.owned(session, inbox_id, owner, lock=True)
            if inbox.retired:
                return
            inbox.retired = True
            inbox.updated_at = datetime.now(UTC)
            inbox.claim = None
            await session.execute(
                update(Subscription)
                .where(Subscription.inbox_id == inbox_id, ~Subscription.cancelled)
                .values(cancelled=True, version=Subscription.version + 1)
            )

    async def claim(self) -> Inbox | None:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Inbox)
                .where(~Inbox.retired, Inbox.next_poll <= func.now(), Inbox.claim_until <= func.now())
                .order_by(Inbox.next_poll)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is not None:
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                row.claim = uuid4()
                row.claim_until = now + timedelta(seconds=30)
                row.next_poll = now + timedelta(seconds=5)
            return row

    async def fenced(self, session: AsyncSession, claim: Inbox) -> Inbox:
        row = await session.scalar(
            select(Inbox)
            .where(
                Inbox.id == claim.id,
                Inbox.claim == claim.claim,
                Inbox.claim_until > func.clock_timestamp(),
                ~Inbox.retired,
            )
            .with_for_update()
        )
        if row is None:
            raise ClaimLostError
        return row

    async def release(self, claim: Inbox, error: str | None) -> None:
        async with self.sessions.begin() as session:
            row = await self.fenced(session, claim)
            row.claim_until = datetime.now(UTC)
            row.delivery_error = error

    async def source(self, claim: Inbox) -> Subscription | None:
        async with self.sessions() as session:
            row: Subscription | None = await session.scalar(
                select(Subscription)
                .where(
                    Subscription.inbox_id == claim.id,
                    ~Subscription.cancelled,
                    ~Subscription.paused,
                    Subscription.expires_at > func.now(),
                    Subscription.next_poll <= func.now(),
                )
                .order_by(Subscription.next_poll)
                .limit(1)
            )
            return row

    async def record(
        self, claim: Inbox, source: Subscription, events: list[ActionEventView], error: str | None = None
    ) -> None:
        async with self.sessions.begin() as session:
            inbox = await self.fenced(session, claim)
            row = await session.get(Subscription, source.id)
            assert row is not None
            if row.cancelled or row.paused or row.version != source.version or row.expires_at <= datetime.now(UTC):
                return
            row.error = error
            row.next_poll = datetime.now(UTC) + timedelta(seconds=30 if error else 5)
            for event in events:
                if event.sequence <= row.after_sequence:
                    continue
                if event.sequence != row.after_sequence + 1:
                    raise ConflictError("Action event history has a gap")
                existing = await session.scalar(
                    select(Entry).where(
                        Entry.inbox_id == inbox.id,
                        Entry.request_id == row.request_id,
                        Entry.source_sequence == event.sequence,
                    )
                )
                if existing is None:
                    if inbox.last_cursor >= 10_000:
                        raise QuotaError("inbox has reached its 10000-entry lifetime limit; retire it explicitly")
                    inbox.last_cursor += 1
                    existing = Entry(
                        inbox_id=inbox.id,
                        cursor=inbox.last_cursor,
                        request_id=row.request_id,
                        source_sequence=event.sequence,
                        payload=event.model_dump(mode="json"),
                        created_at=datetime.now(UTC),
                    )
                    session.add(existing)
                session.add(Match(subscription_id=row.id, cursor=existing.cursor))
                row.after_sequence = event.sequence
            inbox.updated_at = datetime.now(UTC) if events else inbox.updated_at

    async def notice(self, claim: Inbox, *, prepare: bool = True) -> Notice | None:
        async with self.sessions.begin() as session:
            inbox = await self.fenced(session, claim)
            row = await session.get(Notice, inbox.id)
            if not prepare or (row is not None and not row.confirmed and row.error is None):
                return row
            start = max(inbox.covered, inbox.acknowledged, inbox.expired_through)
            if start >= inbox.last_cursor:
                return None
            # Retain the latest terminal receipt; older notice coverage remains in `covered`.
            if row is not None:
                await session.delete(row)
                await session.flush()
            row = Notice(
                inbox_id=inbox.id,
                command_id=uuid4(),
                through_cursor=inbox.last_cursor,
                text=f"Agentplane automated notification: {inbox.last_cursor - start} new notifications through cursor {inbox.last_cursor}. Retrieve GET /v1/inboxes/{inbox.id}/entries from the notification service. Read does not acknowledge; explicitly acknowledge only the handled contiguous prefix.",
                attempted=False,
                admitted=False,
                confirmed=False,
                runner_cursor=0,
            )
            session.add(row)
            inbox.covered = inbox.last_cursor
            return row

    async def checkpoint_before_attempt(self, claim: Inbox, notice: Notice, entry: event_log_pb2.EventEntry) -> None:
        async with self.sessions.begin() as session:
            await self.fenced(session, claim)
            row = await session.get(Notice, claim.id)
            assert row is not None
            assert row.command_id == notice.command_id
            if row.attempted:
                raise ConflictError("cannot skip runner history after a delivery attempt")
            if entry.cursor <= row.runner_cursor:
                raise ConflictError("initial checkpoint must advance the runner cursor")
            row.runner_cursor = entry.cursor
            row.runner_entry = entry.SerializeToString(deterministic=True)

    async def attempt(self, claim: Inbox, notice: Notice) -> bool:
        async with self.sessions.begin() as session:
            inbox = await self.fenced(session, claim)
            row = await session.get(Notice, inbox.id)
            assert row is not None
            assert row.command_id == notice.command_id
            if not row.attempted and max(inbox.acknowledged, inbox.expired_through) >= row.through_cursor:
                row.error = "suppressed: acknowledged before submission"
                return False
            row.attempted = True
            return True

    async def receipt(self, claim: Inbox, notice: Notice, entry: event_log_pb2.EventEntry) -> None:
        async with self.sessions.begin() as session:
            await self.fenced(session, claim)
            row = await session.get(Notice, claim.id)
            assert row is not None
            assert row.command_id == notice.command_id
            wire = entry.SerializeToString(deterministic=True)
            if entry.cursor == row.runner_cursor and row.runner_cursor:
                if row.runner_entry != wire:
                    raise ConflictError("runner history changed at the committed boundary")
                return
            if entry.cursor != row.runner_cursor + 1:
                raise ConflictError("runner history has a gap")
            command_id = str(row.command_id)
            event = entry.event
            if event.HasField("command_admitted") and event.command_admitted.command.command_id == command_id:
                if event.command_admitted.command.submit_input.text != row.text:
                    raise ConflictError("runner admitted different notice content")
                row.admitted = True
            if (
                event.HasField("harness_user_message_confirmed")
                and command_id in event.harness_user_message_confirmed.origin_command_ids
            ):
                row.confirmed = True
            if event.HasField("command_failed") and event.command_failed.command_id == command_id:
                row.error = "runner command failed; inspect native runner evidence"
            if event.HasField("command_noop") and event.command_noop.command_id == command_id:
                row.error = "runner command was a no-op; not confirmed"
            row.runner_cursor, row.runner_entry = entry.cursor, wire

    async def cleanup(self, after_id: UUID | None = None) -> UUID | None:
        async with self.sessions.begin() as session:
            # Keep bounded identity tombstones so replay after payload expiry cannot re-notify.
            cutoff = datetime.now(UTC) - timedelta(days=30)
            rows = await session.scalars(
                select(Inbox)
                .where(Inbox.id > (after_id or UUID(int=0)))
                .order_by(Inbox.id)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            last_id = None
            for inbox in rows:
                last_id = inbox.id
                if inbox.retired and inbox.updated_at < cutoff:
                    await session.delete(inbox)
                    continue
                expired = list(
                    await session.scalars(
                        select(Entry)
                        .where(Entry.inbox_id == inbox.id, Entry.cursor > inbox.expired_through)
                        .order_by(Entry.cursor)
                        .limit(1000)
                    )
                )
                for entry in expired:
                    if entry.created_at >= cutoff:
                        break
                    entry.payload = None
                    inbox.expired_through = entry.cursor
            return last_id
