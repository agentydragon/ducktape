"""`ThreadStore`, the thread component's API over PostgreSQL.

A Thread is its row here, with its title, archive state, and title automation state, joined with its
fold and Sandbox identity from the History Service.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import String, and_, cast, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.app.database_updates import Channel
from agentplane.app.threads.events.event_log import ThreadNotFoundError
from agentplane.app.threads.models import Thread
from agentplane.app.threads.summaries import FeedError, FoldSummary, Summaries
from agentplane.app.threads.thread_view import ThreadView
from agentplane.postgres.wakeups import notify
from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.harness import Harness

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

# The History Service's bound on one summary read.
MAX_PAGE = 200


class InvalidPageTokenError(ValueError):
    pass


class HistorySandboxMissingError(LookupError):
    """A committed History Session has no authoritative Sandbox identity."""

    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"History Session {thread_id} has no Sandbox identity")


class ConcurrentTitleUpdateError(RuntimeError):
    """The title changed while automatic title generation was in flight."""


class Listing(BaseModel):
    """The filters a page token was issued for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox: str | None
    sandbox_uid: str | None = None
    search: str | None
    include_archived: bool


class PageToken(BaseModel):
    """The last Thread served, in the listing's order."""

    model_config = ConfigDict(extra="forbid")

    listing: Listing
    created_at: datetime
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, token: str, listing: Listing) -> PageToken:
        try:
            decoded = cls.model_validate_json(urlsafe_b64decode(token.encode()))
        except (Base64Error, ValidationError) as error:
            raise InvalidPageTokenError("malformed Thread page token") from error
        if decoded.listing != listing:
            raise InvalidPageTokenError("Thread page token was issued for another listing")
        return decoded


class ThreadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadView]
    next_page_token: str | None = Field(description="Continues this listing; None on its last page.")


class ThreadStore:
    def __init__(self, engine: AsyncEngine, summaries: Summaries) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._summaries = summaries

    async def list_page(
        self,
        *,
        limit: int,
        page_token: str | None = None,
        sandbox: str | None = None,
        sandbox_uid: str | None = None,
        search: str | None = None,
        include_archived: bool = False,
    ) -> ThreadPage:
        """Up to `limit` Threads, newest first; each filter given narrows the list to Threads matching
        it, and archived Threads are excluded unless asked for. `search` matches a Thread's name or
        Sandbox, case-insensitively, or the start of its ID. A page token continues only the listing
        it was issued for."""
        if not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"page size must be between 1 and {MAX_PAGE}: {limit=}")
        listing = Listing(sandbox=sandbox, sandbox_uid=sandbox_uid, search=search, include_archived=include_archived)
        query = select(Thread).order_by(Thread.created_at.desc(), Thread.id.desc()).limit(limit + 1)
        if sandbox is not None:
            query = query.where(Thread.id.in_(await self._find(sandbox_name=sandbox)))
        if sandbox_uid is not None:
            query = query.where(Thread.id.in_(await self._find(sandbox_uid=sandbox_uid)))
        if search is not None:
            query = query.where(
                or_(
                    Thread.name.icontains(search, autoescape=True),
                    Thread.id.in_(await self._find(sandbox_name_contains=search)),
                    cast(Thread.id, String).startswith(search.lower(), autoescape=True),
                )
            )
        if not include_archived:
            query = query.where(Thread.archived.is_(False))
        if page_token is not None:
            after = PageToken.decode(page_token, listing)
            query = query.where(tuple_(Thread.created_at, Thread.id) < (after.created_at, after.id))
        async with self._sessions() as session:
            rows = list(await session.scalars(query))
        page = rows[:limit]
        ids = {thread.id for thread in page}
        summaries = await self._summaries.many(ids)
        last = page[-1] if len(rows) > limit else None
        return ThreadPage(
            threads=[self._view(thread, summaries[thread.id]) for thread in page if summaries[thread.id].present],
            next_page_token=None
            if last is None
            else PageToken(listing=listing, created_at=last.created_at, id=last.id).encode(),
        )

    async def list_threads(
        self, *, sandbox: str | None = None, sandbox_uid: str | None = None, include_archived: bool = False
    ) -> list[ThreadView]:
        """Every Thread `list_page` would list, page by page."""
        threads: list[ThreadView] = []
        page_token = None
        while True:
            page = await self.list_page(
                limit=MAX_PAGE,
                page_token=page_token,
                sandbox=sandbox,
                sandbox_uid=sandbox_uid,
                include_archived=include_archived,
            )
            threads.extend(page.threads)
            if page.next_page_token is None:
                return threads
            page_token = page.next_page_token

    async def _find(self, **match: str) -> set[UUID]:
        return await self._summaries.find(**match)

    @staticmethod
    def _view(thread: Thread, summary: FoldSummary) -> ThreadView:
        if not summary.present:
            raise ThreadNotFoundError(thread.id)
        if summary.sandbox is None:
            raise HistorySandboxMissingError(thread.id)
        return _view(thread, summary, summary.sandbox, summary.sandbox_uid)

    async def archived_count(self) -> int:
        async with self._sessions() as session:
            return (await session.scalar(select(func.count()).select_from(Thread).where(Thread.archived))) or 0

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            thread = await session.get(Thread, thread_id)
        if thread is None:
            return None
        summary = await self._summaries.get(thread.id)
        return None if not summary.present else self._view(thread, summary)

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        summary = await self._require_summary(thread_id)
        async with self._sessions.begin() as session:
            thread = await _set_thread(
                session,
                thread_id,
                name=name,
                title_user_managed=True,
                title_status="done",
                title_claimed_at=None,
                title_revision=Thread.title_revision + 1,
            )
            await notify(session, Channel.THREADS)
        return self._view(thread, summary)

    async def open_thread(self, thread_id: UUID, *, auto_title_on_first_message: bool) -> None:
        """Create the app-owned row before a new Session can receive a user message."""
        async with self._sessions.begin() as session:
            created = await session.scalar(
                insert(Thread)
                .values(id=thread_id, auto_title_on_first_message=auto_title_on_first_message)
                .on_conflict_do_nothing()
                .returning(Thread.id)
            )
            if created is not None:
                await notify(session, Channel.THREADS)

    async def pending_auto_titles(self) -> list[tuple[UUID, int]]:
        """Threads whose first-message title still needs scanning or a crashed claim needs recovery."""
        now = datetime.now(UTC)
        stale_before = now - timedelta(minutes=5)
        pending = or_(
            Thread.title_status == "waiting",
            and_(Thread.title_status == "generating", Thread.title_claimed_at < stale_before),
        )
        async with self._sessions() as session:
            rows = await session.execute(
                select(Thread.id, Thread.title_scan_cursor)
                .where(Thread.auto_title_on_first_message, ~Thread.title_user_managed, Thread.name.is_(None), pending)
                .order_by(Thread.created_at)
            )
            return [(row.id, row.title_scan_cursor) for row in rows]

    async def advance_title_scan_cursor(self, thread_id: UUID, cursor: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id, Thread.title_status == "waiting")
                .values(title_scan_cursor=cursor)
            )

    async def claim_auto_title(self, thread_id: UUID, cursor: int) -> int | None:
        """Claim one first-message generation across replicas, recovering stale claims after a crash."""
        now = datetime.now(UTC)
        stale_before = now - timedelta(minutes=5)
        claimable = or_(
            Thread.title_status == "waiting",
            and_(Thread.title_status == "generating", Thread.title_claimed_at < stale_before),
        )
        async with self._sessions.begin() as session:
            return await session.scalar(
                update(Thread)
                .where(
                    Thread.id == thread_id,
                    Thread.auto_title_on_first_message,
                    ~Thread.title_user_managed,
                    Thread.name.is_(None),
                    claimable,
                )
                .values(title_status="generating", title_claimed_at=now, title_scan_cursor=cursor)
                .returning(Thread.title_revision)
            )

    async def generated_title(self, thread_id: UUID, title: str, *, expected_revision: int) -> ThreadView:
        """Apply an on-demand title only if no user or other generation changed it meanwhile."""
        summary = await self._require_summary(thread_id)
        async with self._sessions.begin() as session:
            thread = await session.scalar(
                update(Thread)
                .where(Thread.id == thread_id, Thread.title_revision == expected_revision)
                .values(
                    name=title, title_status="done", title_claimed_at=None, title_revision=Thread.title_revision + 1
                )
                .returning(Thread)
            )
            if thread is None:
                raise ConcurrentTitleUpdateError(f"Thread {thread_id} was renamed while generating its title")
            await notify(session, Channel.THREADS)
        return self._view(thread, summary)

    async def apply_first_message_title(self, thread_id: UUID, title: str, *, expected_revision: int) -> bool:
        """Apply a claimed title only while the Thread is still unnamed and unedited."""
        async with self._sessions.begin() as session:
            updated = await session.scalar(
                update(Thread)
                .where(
                    Thread.id == thread_id,
                    Thread.title_status == "generating",
                    Thread.title_revision == expected_revision,
                    ~Thread.title_user_managed,
                    Thread.name.is_(None),
                )
                .values(
                    name=title, title_status="done", title_claimed_at=None, title_revision=Thread.title_revision + 1
                )
                .returning(Thread.id)
            )
            if updated is not None:
                await notify(session, Channel.THREADS)
        return updated is not None

    async def fail_auto_title(self, thread_id: UUID, *, expected_revision: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(Thread)
                .where(
                    Thread.id == thread_id,
                    Thread.title_status == "generating",
                    Thread.title_revision == expected_revision,
                )
                .values(title_status="failed", title_claimed_at=None)
            )

    async def title_context(self, thread_id: UUID) -> tuple[int, str | None]:
        async with self._sessions() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            return thread.title_revision, thread.name

    async def archive(self, thread_id: UUID) -> ThreadView:
        """Hide the thread from a default listing without touching its events."""
        return await self._set_archived(thread_id, True)

    async def unarchive(self, thread_id: UUID) -> ThreadView:
        return await self._set_archived(thread_id, False)

    async def _set_archived(self, thread_id: UUID, archived: bool) -> ThreadView:
        summary = await self._require_summary(thread_id)
        async with self._sessions.begin() as session:
            thread = await _set_thread(session, thread_id, archived=archived)
            await notify(session, Channel.THREADS)
        return self._view(thread, summary)

    async def _require_summary(self, thread_id: UUID) -> FoldSummary:
        summary = await self._summaries.get(thread_id)
        if not summary.present:
            raise ThreadNotFoundError(thread_id)
        if summary.sandbox is None:
            raise HistorySandboxMissingError(thread_id)
        return summary


async def _set_thread(session: AsyncSession, thread_id: UUID, **values: object) -> Thread:
    """Write what an operator set on a Thread."""
    thread = await session.scalar(update(Thread).where(Thread.id == thread_id).values(**values).returning(Thread))
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    return thread


def _view(thread: Thread, summary: FoldSummary, sandbox: str, sandbox_uid: str | None) -> ThreadView:
    last_turn_status = None if summary.last_turn_status is None else event_pb2.TurnStatus.Name(summary.last_turn_status)
    attachment = summary.feed.attached if summary.feed is not None else None
    end = summary.feed.end if summary.feed is not None else None
    harness_state = attachment.harness_state if attachment is not None else runner_pb2.HARNESS_STATE_UNSPECIFIED
    spec = attachment.spec if attachment is not None and attachment.HasField("spec") else None
    return ThreadView(
        id=thread.id,
        sandbox=sandbox,
        sandbox_uid=None if sandbox_uid is None else UUID(sandbox_uid),
        harness=None if spec is None else Harness(runner_pb2.Harness.Name(spec.harness)),
        model=None if spec is None else spec.model,
        cwd=None if spec is None else spec.cwd,
        created_at=thread.created_at,
        name=thread.name,
        archived=thread.archived,
        last_cursor=summary.folded_through,
        last_event_at=summary.last_event_at,
        last_model_activity_at=summary.last_model_activity_at,
        idle_since=summary.idle_since,
        harness_state=runner_pb2.HarnessState.Name(harness_state),
        setup_state=runner_pb2.SetupState.Name(attachment.setup_state) if attachment is not None else None,
        launch_outcome=runner_pb2.LaunchOutcome.Name(attachment.launch_outcome) if attachment is not None else None,
        active_turn_id=(attachment.active_turn_id or None) if attachment is not None else None,
        feed_status=(
            None
            if attachment is None
            else "active"
            if end is None
            else "failed"
            if isinstance(end, FeedError)
            else "ended"
        ),
        reasoning_effort=attachment.spec.reasoning_effort if attachment is not None else None,
        last_turn_status=last_turn_status,
    )
