"""Short fenced transactions; no network calls while holding the inbox prefix lock."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import ColumnElement, case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.action_service.models import ActionEventView
from agentplane.notification_service.db import (
    Entry,
    GitHubDelivery,
    GitHubDeliverySubject,
    GitHubInstallation,
    GitHubRepository,
    GitHubRepositoryAccess,
    GitHubSubject,
    GitHubSubjectRevision,
    Inbox,
    Match,
    Notice,
    Subscription,
)
from agentplane.notification_service.github_state import (
    AccessFence,
    HeadRevision,
    RefreshDeferredError,
    RefreshRow,
    SubjectKey,
    access_valid,
    add_revisions,
    fences_valid,
    github_lock,
)
from agentplane.notification_service.models import (
    ActionsEvent,
    ActionsSource,
    EntryView,
    EventIdentity,
    GitHubAccessStatus,
    GitHubRefreshStatus,
    GitHubStatus,
    GitHubSubjectStatus,
    InboxEntrySummary,
    InboxPage,
    InboxStatus,
    InboxView,
    NoticeView,
    SandboxNotificationStatus,
    SourceFailureKind,
    Subscribe,
    SubscriptionStatus,
    SubscriptionUpdate,
    SubscriptionView,
)
from agentplane.notification_service.sources.github_models import GitHubBinding, GitHubEvent, GitHubSource, subject_key
from agentplane.notification_service.updates import Wakeups, notify
from agentplane.protocol import event_log_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipal

# gazelle:include_dep @pypi//protobuf


_EVENT: TypeAdapter[EventIdentity] = TypeAdapter(EventIdentity)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class QuotaError(Exception):
    pass


class ClaimLostError(Exception):
    pass


def refresh_status(row: RefreshRow) -> GitHubRefreshStatus:
    return GitHubRefreshStatus(
        last_success_at=row.last_success_at,
        error_kind=SourceFailureKind(row.error_kind) if row.error_kind else None,
        error=row.error,
        error_since=row.error_since,
        error_observed_at=row.error_observed_at,
        retry_at=row.next_attempt if row.error is not None else None,
        refreshing_until=row.claim_until if row.claim is not None else None,
    )


async def github_status(session: AsyncSession, row: Subscription) -> GitHubStatus | None:
    if row.github_app_id is None:
        return None
    revisions = select(GitHubSubjectRevision.head_repository_id).where(
        GitHubSubjectRevision.repository_id == row.github_repository_id,
        GitHubSubjectRevision.kind == row.github_subject_kind,
        GitHubSubjectRevision.subject_key == row.github_subject_key,
    )
    accesses = await session.scalars(
        select(GitHubRepositoryAccess)
        .where(
            GitHubRepositoryAccess.app_id == row.github_app_id,
            (
                (GitHubRepositoryAccess.repository_id == row.github_repository_id)
                & (GitHubRepositoryAccess.installation_id == row.github_installation_id)
            )
            | GitHubRepositoryAccess.repository_id.in_(revisions),
        )
        .order_by(GitHubRepositoryAccess.repository_id, GitHubRepositoryAccess.installation_id)
    )
    subject = await session.get(
        GitHubSubject, (row.github_repository_id, row.github_subject_kind, row.github_subject_key)
    )
    assert subject is not None
    return GitHubStatus(
        access=[
            GitHubAccessStatus(
                **refresh_status(access).model_dump(),
                app_id=access.app_id,
                installation_id=access.installation_id,
                repository_id=access.repository_id,
                checked_at=access.checked_at,
                valid_until=access.valid_until,
                currently_valid=await access_valid(session, access, datetime.now(UTC)),
            )
            for access in accesses
        ],
        subject=GitHubSubjectStatus(
            **refresh_status(subject).model_dump(),
            repository_id=subject.repository_id,
            kind=subject.kind,
            subject_key=subject.subject_key,
        ),
    )


async def subscription_view(session: AsyncSession, row: Subscription) -> SubscriptionView:
    return SubscriptionView(
        github=await github_status(session, row),
        id=row.id,
        inbox_id=row.inbox_id,
        source=Subscribe.model_validate(row.creation).source,
        idempotency_key=row.idempotency_key,
        version=row.version,
        cancelled=row.cancelled,
        expires_at=row.expires_at,
        last_success_at=row.last_success_at,
        error_kind=SourceFailureKind(row.error_kind) if row.error_kind else None,
        error_since=row.error_since,
        error_observed_at=row.error_observed_at,
        error=row.error,
        retry_at=row.next_attempt if row.error and not row.cancelled and row.expires_at > datetime.now(UTC) else None,
    )


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self.wakeups = Wakeups(engine.url)
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

    async def subscribe(
        self, principal: WorkloadPrincipal, body: Subscribe, binding: GitHubBinding | None = None
    ) -> SubscriptionView:
        owner = principal.account
        now = datetime.now(UTC)
        key = hashlib.sha256(
            json.dumps([body.destination_ref.model_dump(), body.session_id], sort_keys=True).encode()
        ).hexdigest()
        async with self.sessions.begin() as session:
            if isinstance(body.source, GitHubSource):
                await github_lock(session)
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
                    next_attempt=now,
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
                    Subscription.inbox_id == inbox.id, Subscription.idempotency_key == body.idempotency_key
                )
            )
            if row is not None:
                if Subscribe.model_validate(row.creation) != body:
                    raise ConflictError("idempotency key already names another subscription")
                return await subscription_view(session, row)
            count = await session.scalar(
                select(func.count()).select_from(Subscription).where(Subscription.inbox_id == inbox.id)
            )
            if count is not None and count >= 64:
                raise QuotaError("64 subscriptions per inbox, including cancelled subscriptions")
            actions_after_sequence: int | None
            github_start_position: int | None
            if isinstance(body.source, ActionsSource):
                actions_after_sequence = body.source.after_sequence
                github_start_position = None
            else:
                if binding is None:
                    raise ConflictError("GitHub repository must be authorized before subscribing")
                actions_after_sequence = None
                github_start_position = await session.scalar(
                    select(func.coalesce(func.max(GitHubDelivery.position), 0))
                )
                assert github_start_position is not None
                await session.execute(
                    insert(GitHubInstallation)
                    .values(app_id=binding.app_id, installation_id=binding.installation_id, generation=0)
                    .on_conflict_do_nothing()
                )
                await session.execute(
                    insert(GitHubRepository)
                    .values(repository_id=binding.repository_id, full_name=body.source.repository)
                    .on_conflict_do_nothing()
                )
                await session.execute(
                    insert(GitHubRepositoryAccess)
                    .values(
                        app_id=binding.app_id,
                        installation_id=binding.installation_id,
                        repository_id=binding.repository_id,
                        generation=0,
                    )
                    .on_conflict_do_nothing()
                )
                await session.execute(
                    insert(GitHubSubject)
                    .values(
                        repository_id=binding.repository_id,
                        kind=body.source.subject.kind,
                        subject_key=subject_key(body.source.subject),
                        generation=0,
                    )
                    .on_conflict_do_nothing()
                )
            row = Subscription(
                id=uuid4(),
                inbox_id=inbox.id,
                idempotency_key=body.idempotency_key,
                creation=body.model_dump(mode="json"),
                creator=asdict(principal),
                version=1,
                actions_after_sequence=actions_after_sequence,
                github_start_position=github_start_position,
                github_app_id=binding.app_id if binding else None,
                github_installation_id=binding.installation_id if binding else None,
                github_repository_id=binding.repository_id if binding else None,
                github_subject_kind=body.source.subject.kind if isinstance(body.source, GitHubSource) else None,
                github_subject_key=subject_key(body.source.subject) if isinstance(body.source, GitHubSource) else None,
                cancelled=False,
                expires_at=now + timedelta(days=body.lifetime_days),
                next_attempt=now,
            )
            session.add(row)
            inbox.updated_at = now
            inbox.next_attempt = now
            await notify(session)
            await session.flush()
            return await subscription_view(session, row)

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
            return [await subscription_view(session, row) for row in rows]

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
            return await subscription_view(session, row)

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
                    return await subscription_view(session, row)
                row.cancelled = True
            else:
                if row.version != update.version or row.cancelled:
                    raise ConflictError("subscription version changed or subscription cancelled")
                row.expires_at = datetime.now(UTC) + timedelta(days=update.lifetime_days)
                row.next_attempt = datetime.now(UTC)
            row.version += 1
            inbox.next_attempt = datetime.now(UTC)
            await notify(session)
            return await subscription_view(session, row)

    async def sandbox_status(
        self, namespace: str, name: str, uid: str, *, quiet_seconds: float, max_wait_seconds: float
    ) -> SandboxNotificationStatus:
        """Read-only UID-pinned diagnostic projection with bounded, curated entry summaries."""
        async with self.sessions.begin() as session:
            observed = datetime.now(UTC)
            rows = (
                await session.scalars(
                    select(Inbox)
                    .where(
                        Inbox.destination_ref["namespace"].astext == namespace,
                        Inbox.destination_ref["name"].astext == name,
                        Inbox.destination_ref["uid"].astext == uid,
                    )
                    .order_by(Inbox.session_id, Inbox.id)
                    .limit(1000)
                )
            ).all()
            result = []
            for row in rows:
                subscriptions = (
                    await session.scalars(
                        select(Subscription).where(Subscription.inbox_id == row.id).order_by(Subscription.id)
                    )
                ).all()
                notice = await session.get(Notice, row.id)
                boundary = max(row.covered, row.acknowledged, row.expired_through)
                first, last, unannounced = (
                    await session.execute(
                        select(func.min(Entry.created_at), func.max(Entry.created_at), func.count()).where(
                            Entry.inbox_id == row.id, Entry.cursor > boundary
                        )
                    )
                ).one()
                pending_ack = (
                    await session.execute(
                        select(func.count()).where(
                            Entry.inbox_id == row.id, Entry.cursor > max(row.acknowledged, row.expired_through)
                        )
                    )
                ).scalar_one()
                pending_rows = (
                    await session.scalars(
                        select(Entry)
                        .where(Entry.inbox_id == row.id, Entry.cursor > max(row.acknowledged, row.expired_through))
                        .order_by(Entry.cursor)
                        .limit(101)
                    )
                ).all()
                summaries = []
                for entry in pending_rows[:100]:
                    event = _EVENT.validate_python(entry.event)
                    if isinstance(event, ActionsEvent):
                        # Action state is a provider-owned lifecycle value, not arbitrary result content.
                        state = entry.payload.get("state") if entry.payload else None
                        summary = f"Action {event.request_id} · event {event.sequence}"
                        if isinstance(state, str):
                            summary += f" · {state[:80]}"
                    else:
                        summary = f"GitHub {event.event.value}"
                        if event.action:
                            summary += f" · {event.action[:80]}"
                        if event.event.value in ("check_run", "workflow_run") and entry.payload:
                            check = entry.payload.get(event.event.value)
                            if isinstance(check, dict):
                                conclusion = check.get("conclusion")
                                if isinstance(conclusion, str):
                                    summary += f" · {conclusion[:80]}"
                    summaries.append(
                        InboxEntrySummary(
                            cursor=entry.cursor, created_at=entry.created_at, provider=event.provider, summary=summary
                        )
                    )
                quiet_until = last + timedelta(seconds=quiet_seconds) if last else None
                max_wait_at = first + timedelta(seconds=max_wait_seconds) if first else None
                due = min(quiet_until, max_wait_at) if quiet_until and max_wait_at else None
                wait_reason = (
                    "retired"
                    if row.retired
                    else "notice_error"
                    if notice and notice.error
                    else "delivery_retry"
                    if row.delivery_error
                    else "awaiting_confirmation"
                    if notice and not notice.confirmed
                    else "debouncing"
                    if due and due > observed
                    else "ready"
                    if due
                    else None
                )
                result.append(
                    InboxStatus(
                        inbox=InboxView.model_validate(row),
                        notice=NoticeView.model_validate(notice) if notice else None,
                        subscriptions=[
                            SubscriptionStatus(
                                github=await github_status(session, sub),
                                id=sub.id,
                                source=Subscribe.model_validate(sub.creation).source,
                                cancelled=sub.cancelled,
                                expires_at=sub.expires_at,
                                last_success_at=sub.last_success_at,
                                error_kind=SourceFailureKind(sub.error_kind) if sub.error_kind else None,
                                error_since=sub.error_since,
                                error_observed_at=sub.error_observed_at,
                                error=sub.error,
                                next_source_check_at=sub.next_attempt,
                            )
                            for sub in subscriptions
                        ],
                        unannounced_count=unannounced,
                        pending_acknowledgement_count=pending_ack,
                        pending_entries=summaries,
                        pending_entries_more=pending_ack > len(summaries),
                        notice_due_at=due if wait_reason == "debouncing" else None,
                        quiet_until=quiet_until if wait_reason == "debouncing" else None,
                        max_wait_at=max_wait_at if wait_reason == "debouncing" else None,
                        notice_wait_reason=wait_reason,
                        next_work_at=row.next_attempt,
                    )
                )
            return SandboxNotificationStatus(observed_at=observed, inboxes=result)

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
                        event=_EVENT.validate_python(row.event),
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
            await notify(session)
            return InboxView.model_validate(inbox)

    async def retire(self, owner: ServiceAccountRef, inbox_id: UUID) -> None:
        async with self.sessions.begin() as session:
            inbox = await self.owned(session, inbox_id, owner, lock=True)
            if inbox.retired:
                return
            inbox.retired = True
            inbox.updated_at = datetime.now(UTC)
            inbox.claim = None
            inbox.next_attempt = None
            await session.execute(
                update(Subscription)
                .where(Subscription.inbox_id == inbox_id, ~Subscription.cancelled)
                .values(cancelled=True, version=Subscription.version + 1)
            )
            await notify(session)

    async def observe_stale(self, claim: Inbox, reason: str, confirmation_s: float) -> bool:
        """Release this claim with a durable recheck, or retire a confirmed stale incarnation."""
        async with self.sessions.begin() as session:
            row = await self.fenced(session, claim)
            now = datetime.now(UTC)
            if row.stale_check_at is None:
                row.stale_check_at = now + timedelta(seconds=confirmation_s)
            elif row.stale_check_at <= now:
                row.retired = True
                row.updated_at = now
                row.next_attempt = None
                row.claim = None
                row.claim_until = now
                row.delivery_error = f"destination retired: {reason}"
                await session.execute(
                    update(Subscription)
                    .where(Subscription.inbox_id == row.id, ~Subscription.cancelled)
                    .values(cancelled=True, version=Subscription.version + 1)
                )
                await notify(session)
                return True
            row.next_attempt = row.stale_check_at
            row.claim_until = now
            row.delivery_error = f"destination pending retirement: {reason}"
            await notify(session)
            return False

    async def clear_stale(self, claim: Inbox) -> None:
        if claim.stale_check_at is not None:
            async with self.sessions.begin() as session:
                row = await self.fenced(session, claim)
                row.stale_check_at = None

    async def claim(self) -> Inbox | None:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Inbox)
                .where(~Inbox.retired, Inbox.next_attempt <= func.now(), Inbox.claim_until <= func.now())
                .order_by(Inbox.next_attempt)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is not None:
                now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                row.claim = uuid4()
                row.claim_until = now + timedelta(seconds=30)
                await notify(session)  # Other replicas must account for this lease's expiry.
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

    async def release(self, claim: Inbox, error: str | None, *, notice_due_at: datetime | None = None) -> None:
        async with self.sessions.begin() as session:
            row = await self.fenced(session, claim)
            now = datetime.now(UTC)
            row.claim_until = now
            row.delivery_error = error
            next_source = await session.scalar(
                select(func.min(Subscription.next_attempt)).where(
                    Subscription.inbox_id == row.id, ~Subscription.cancelled, Subscription.expires_at > func.now()
                )
            )
            notice = await session.get(Notice, row.id)
            waiting = notice is not None and not notice.confirmed and notice.error is None
            unread = row.last_cursor > max(row.covered, row.acknowledged, row.expired_through)
            deadlines = [next_source] if next_source is not None else []
            if waiting:
                deadlines.append(now + timedelta(seconds=5))
            elif unread:
                deadlines.append(notice_due_at if notice_due_at is not None else now + timedelta(seconds=5))
            row.next_attempt = min(deadlines) if deadlines else None
            if error is not None and row.next_attempt is not None:
                row.next_attempt = max(row.next_attempt, now + timedelta(seconds=5))
            if row.stale_check_at is not None:
                # A transient lookup failure must not confirm staleness or spin before the recheck.
                row.next_attempt = max(row.stale_check_at, now + timedelta(seconds=5) if error is not None else now)
            await notify(session)

    async def get_next_work_at(self) -> datetime | None:
        """Next durable retry/source/lease deadline, not an interval for checking the queue."""
        async with self.sessions() as session:
            deadline: datetime | None = await session.scalar(
                select(func.min(func.greatest(Inbox.next_attempt, Inbox.claim_until))).where(
                    ~Inbox.retired, Inbox.next_attempt.is_not(None)
                )
            )
            return deadline

    async def source(self, claim: Inbox) -> Subscription | None:
        async with self.sessions() as session:
            row: Subscription | None = await session.scalar(
                select(Subscription)
                .where(
                    Subscription.inbox_id == claim.id,
                    ~Subscription.cancelled,
                    Subscription.expires_at > func.now(),
                    Subscription.next_attempt <= func.now(),
                )
                .order_by(Subscription.next_attempt)
                .limit(1)
            )
            return row

    async def record(self, claim: Inbox, source: Subscription, events: list[ActionEventView]) -> None:
        async with self.sessions.begin() as session:
            inbox = await self.fenced(session, claim)
            row = await session.get(Subscription, source.id)
            assert row is not None
            if row.cancelled or row.version != source.version or row.expires_at <= datetime.now(UTC):
                return
            row.next_attempt = datetime.now(UTC) + timedelta(seconds=5)
            self.source_succeeded(row)
            spec = Subscribe.model_validate(row.creation).source
            assert isinstance(spec, ActionsSource)
            assert row.actions_after_sequence is not None
            for event in events:
                if event.sequence <= row.actions_after_sequence:
                    continue
                if event.sequence != row.actions_after_sequence + 1:
                    raise ConflictError("Action event history has a gap")
                await self.append_event(
                    session,
                    inbox,
                    row,
                    ActionsEvent(provider="actions", request_id=spec.request_id, sequence=event.sequence),
                    event.model_dump(mode="json"),
                )
                row.actions_after_sequence = event.sequence
            inbox.updated_at = datetime.now(UTC) if events else inbox.updated_at
            await notify(session)

    async def append_event(
        self,
        session: AsyncSession,
        inbox: Inbox,
        subscription: Subscription,
        identity: EventIdentity,
        payload: dict[str, JsonValue],
    ) -> None:
        event = identity.model_dump(mode="json")
        existing = await session.scalar(select(Entry).where(Entry.inbox_id == inbox.id, Entry.event == event))
        if existing is None:
            if inbox.last_cursor >= 10_000:
                raise QuotaError("inbox has reached its 10000-entry lifetime limit; retire it explicitly")
            inbox.last_cursor += 1
            existing = Entry(
                inbox_id=inbox.id, cursor=inbox.last_cursor, event=event, payload=payload, created_at=datetime.now(UTC)
            )
            session.add(existing)
        if await session.get(Match, (subscription.id, existing.cursor)) is None:
            session.add(Match(subscription_id=subscription.id, cursor=existing.cursor))

    @staticmethod
    def source_succeeded(row: Subscription) -> None:
        row.last_success_at = datetime.now(UTC)
        row.error_kind = None
        row.error = None
        row.error_since = None
        row.error_observed_at = None

    async def source_failed(
        self,
        claim: Inbox,
        source: Subscription,
        error: str,
        retry_seconds: int = 60,
        *,
        kind: SourceFailureKind = SourceFailureKind.PROCESSING_ERROR,
    ) -> None:
        async with self.sessions.begin() as session:
            await self.fenced(session, claim)
            row = await session.get(Subscription, source.id)
            assert row is not None
            if not row.cancelled and row.version == source.version and row.expires_at > datetime.now(UTC):
                now = datetime.now(UTC)
                row.next_attempt = now + timedelta(seconds=retry_seconds)
                if row.error_kind != kind:
                    row.error_since = now
                row.error_kind = kind
                row.error = error
                row.error_observed_at = now
                await notify(session)

    async def ingest_github(
        self,
        app_id: int,
        delivery_id: UUID,
        installation_id: int,
        repository_id: int | None,
        event: str,
        digest: bytes,
        payload: dict[str, JsonValue],
        *,
        action: str | None,
        head_sha: str | None,
        subjects: Sequence[SubjectKey],
        repository_name: str | None = None,
        revisions: Sequence[HeadRevision] = (),
    ) -> bool:
        async with self.sessions.begin() as session:
            # Allocate identities in committed order, also fencing the subscription start boundary.
            await github_lock(session)
            existing = await session.scalar(
                select(GitHubDelivery).where(GitHubDelivery.app_id == app_id, GitHubDelivery.delivery_id == delivery_id)
            )
            if existing is not None:
                if existing.digest != digest:
                    raise ConflictError("GitHub delivery ID reused with different content")
                return False
            await session.execute(
                insert(GitHubInstallation)
                .values(app_id=app_id, installation_id=installation_id)
                .on_conflict_do_nothing()
            )
            if event in ("installation", "installation_repositories"):
                await session.execute(
                    update(GitHubInstallation)
                    .where(GitHubInstallation.app_id == app_id, GitHubInstallation.installation_id == installation_id)
                    .values(generation=GitHubInstallation.generation + 1)
                )
                affected = select(GitHubRepositoryAccess.repository_id).where(
                    GitHubRepositoryAccess.app_id == app_id, GitHubRepositoryAccess.installation_id == installation_id
                )
                await session.execute(
                    update(GitHubSubject)
                    .where(GitHubSubject.repository_id.in_(affected))
                    .values(generation=GitHubSubject.generation + 1, last_success_at=None, claim=None, claim_until=None)
                )
                await session.execute(
                    update(GitHubRepositoryAccess)
                    .where(
                        GitHubRepositoryAccess.app_id == app_id,
                        GitHubRepositoryAccess.installation_id == installation_id,
                    )
                    .values(
                        generation=GitHubRepositoryAccess.generation + 1, valid_until=None, claim=None, claim_until=None
                    )
                )
            if repository_id is not None and repository_name is not None:
                await session.execute(
                    insert(GitHubRepository)
                    .values(repository_id=repository_id, full_name=repository_name)
                    .on_conflict_do_nothing()
                )
            for key in subjects:
                await session.execute(
                    insert(GitHubSubject)
                    .values(repository_id=key.repository_id, kind=key.kind, subject_key=key.subject_key)
                    .on_conflict_do_nothing()
                )
                await add_revisions(session, key, revisions)
            receipt = GitHubDelivery(
                app_id=app_id,
                delivery_id=delivery_id,
                installation_id=installation_id,
                repository_id=repository_id,
                event=event,
                digest=digest,
                payload=payload,
                action=action,
                head_sha=head_sha,
                received_at=datetime.now(UTC),
            )
            session.add(receipt)
            await session.flush()
            for key in subjects:
                await session.execute(
                    insert(GitHubDeliverySubject)
                    .values(
                        delivery_position=receipt.position,
                        repository_id=key.repository_id,
                        kind=key.kind,
                        subject_key=key.subject_key,
                    )
                    .on_conflict_do_nothing()
                )
            now = datetime.now(UTC)
            active = (
                (Subscription.github_app_id == app_id)
                & ~Subscription.cancelled
                & (Subscription.expires_at > func.now())
            )
            inboxes = await session.scalars(
                select(Inbox)
                .where(~Inbox.retired, Inbox.id.in_(select(Subscription.inbox_id).where(active)))
                .order_by(Inbox.id)
                .with_for_update()
            )
            for inbox in inboxes:
                # Conservatively wake all this App's sources, including PRs whose head lives in a fork.
                await session.execute(
                    update(Subscription)
                    .where(active, Subscription.inbox_id == inbox.id)
                    .values(
                        generation=Subscription.generation + 1,
                        next_attempt=func.coalesce(
                            # Preserve explicit provider-error backoff, otherwise schedule immediately.
                            case((Subscription.error.is_not(None), Subscription.next_attempt), else_=None),
                            now,
                        ),
                    )
                )
                inbox.next_attempt = now
            await notify(session)
            return True

    async def github_deliveries(self, source: Subscription, predicate: ColumnElement[bool]) -> list[GitHubDelivery]:
        assert source.github_start_position is not None
        # Compare the complete identity so the inbox/event unique index serves replay exclusion.
        identity = func.jsonb_build_object(
            "provider",
            "github",
            "app_id",
            GitHubDelivery.app_id,
            "delivery_id",
            GitHubDelivery.delivery_id,
            "repository_id",
            GitHubDelivery.repository_id,
            "event",
            GitHubDelivery.event,
            "action",
            GitHubDelivery.action,
        )
        delivered = (
            select(Match.subscription_id)
            .join(Entry, (Entry.inbox_id == source.inbox_id) & (Entry.cursor == Match.cursor))
            .where(Match.subscription_id == source.id, Entry.event == identity)
            .exists()
        )
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(GitHubDelivery)
                    .where(predicate, GitHubDelivery.position > source.github_start_position, ~delivered)
                    .order_by(GitHubDelivery.position)
                    .limit(128)
                )
            )

    async def record_github(
        self,
        claim: Inbox,
        source: Subscription,
        matched: list[tuple[GitHubDelivery, GitHubEvent]],
        *,
        more: bool,
        fences: Sequence[AccessFence],
        subject_key: SubjectKey,
        subject_generation: int,
        repair_at: datetime,
    ) -> None:
        async with self.sessions.begin() as session:
            await github_lock(session)
            inbox = await self.fenced(session, claim)
            row = await session.get(Subscription, source.id)
            assert row is not None
            if row.cancelled or row.version != source.version or row.expires_at <= datetime.now(UTC):
                return
            assert isinstance(Subscribe.model_validate(row.creation).source, GitHubSource)
            subject = await subject_key.load(session)
            if (
                not fences
                or repair_at <= datetime.now(UTC)
                or subject.generation != subject_generation
                or not await fences_valid(session, fences)
            ):
                row.next_attempt = datetime.now(UTC)
                await notify(session)
                return
            for delivery, identity in matched:
                await self.append_event(session, inbox, row, identity, delivery.payload)
            # An ingress commit during matching must not be overwritten by this worker's idle state.
            row.next_attempt = datetime.now(UTC) if more or row.generation != source.generation else repair_at
            self.source_succeeded(row)
            if matched:
                inbox.updated_at = datetime.now(UTC)
            await session.flush()
            if repair_at <= datetime.now(UTC) or not await fences_valid(session, fences):
                # Roll back the entire appended prefix if access expired while writing the page.
                raise RefreshDeferredError(datetime.now(UTC))
            await notify(session)

    async def source_deferred(self, claim: Inbox, source: Subscription, until: datetime) -> None:
        async with self.sessions.begin() as session:
            await self.fenced(session, claim)
            row = await session.get(Subscription, source.id)
            assert row is not None
            if not row.cancelled and row.version == source.version:
                row.next_attempt = until
                await notify(session)

    async def get_pending_entry_times(self, claim: Inbox) -> tuple[datetime, datetime] | None:
        """Timestamp bounds of entries not yet covered, acknowledged or expired."""
        async with self.sessions.begin() as session:
            inbox = await self.fenced(session, claim)
            first, last = (
                await session.execute(
                    select(func.min(Entry.created_at), func.max(Entry.created_at)).where(
                        Entry.inbox_id == inbox.id,
                        Entry.cursor > max(inbox.covered, inbox.acknowledged, inbox.expired_through),
                    )
                )
            ).one()
            if first is None:
                return None
            assert last is not None
            return first, last

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
            # Both cursors are preparation-time snapshots; read from acknowledgement, not notice coverage.
            row = Notice(
                inbox_id=inbox.id,
                command_id=uuid4(),
                through_cursor=inbox.last_cursor,
                # Standing instructions carry the shared read/ack procedure. Persist the exact
                # snapshot hint once so lost responses replay the same command and cursors.
                text="Agentplane inbox notice: "
                + json.dumps(
                    {
                        "inbox_id": str(inbox.id),
                        "acknowledged_at_preparation": inbox.acknowledged,
                        "through_at_preparation": inbox.last_cursor,
                    },
                    separators=(",", ":"),
                ),
                attempted=False,
                admitted=False,
                confirmed=False,
                runner_cursor=0,
            )
            session.add(row)
            inbox.covered = inbox.last_cursor
            await notify(session)
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
                await notify(session)
                return False
            row.attempted = True
            await notify(session)
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
            await notify(session)

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
