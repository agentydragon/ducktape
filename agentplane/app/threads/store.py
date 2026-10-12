"""`ThreadStore`, the thread component's API over PostgreSQL.

A Thread is its row here, with what an operator set on it (its name and archive state), joined with
its fold from the History Service and the Sandbox the Runner Gateway routes its Session to.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import String, cast, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.app.database_updates import Channel
from agentplane.app.threads.events.event_log import SessionNotRegisteredError, ThreadNotFoundError
from agentplane.app.threads.models import Thread
from agentplane.app.threads.summaries import FeedError, FoldSummary, Summaries
from agentplane.app.threads.thread_view import ThreadView
from agentplane.postgres.wakeups import notify
from agentplane.proto import event_pb2, runner_gateway_pb2, runner_pb2
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.client import RunnerGatewayClient

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

# The History Service's bound on one summary read.
MAX_PAGE = 200


class InvalidPageTokenError(ValueError):
    pass


class Listing(BaseModel):
    """The filters a page token was issued for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox: str | None
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
    def __init__(self, engine: AsyncEngine, summaries: Summaries, *, gateway: RunnerGatewayClient) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._summaries = summaries
        self._gateway = gateway

    async def list_page(
        self,
        *,
        limit: int,
        page_token: str | None = None,
        sandbox: str | None = None,
        search: str | None = None,
        include_archived: bool = False,
    ) -> ThreadPage:
        """Up to `limit` Threads, newest first; each filter given narrows the list to Threads matching
        it, and archived Threads are excluded unless asked for. `search` matches a Thread's name or
        Sandbox, case-insensitively, or the start of its ID. A page token continues only the listing
        it was issued for."""
        if not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"page size must be between 1 and {MAX_PAGE}: {limit=}")
        listing = Listing(sandbox=sandbox, search=search, include_archived=include_archived)
        query = select(Thread).order_by(Thread.created_at.desc(), Thread.id.desc()).limit(limit + 1)
        if sandbox is not None:
            query = query.where(Thread.id.in_(await self._find(sandbox_name=sandbox)))
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
        summaries, sandboxes = await self._summaries.many(ids), await self._sandboxes(ids)
        last = page[-1] if len(rows) > limit else None
        return ThreadPage(
            threads=[_view(thread, summaries[thread.id], sandboxes[thread.id]) for thread in page],
            next_page_token=None
            if last is None
            else PageToken(listing=listing, created_at=last.created_at, id=last.id).encode(),
        )

    async def list_threads(self, *, sandbox: str | None = None, include_archived: bool = False) -> list[ThreadView]:
        """Every Thread `list_page` would list, page by page."""
        threads: list[ThreadView] = []
        page_token = None
        while True:
            page = await self.list_page(
                limit=MAX_PAGE, page_token=page_token, sandbox=sandbox, include_archived=include_archived
            )
            threads.extend(page.threads)
            if page.next_page_token is None:
                return threads
            page_token = page.next_page_token

    async def _find(self, **match: str) -> set[UUID]:
        found = await self._gateway.find_sessions(runner_gateway_pb2.FindSessionsRequest(**match))
        return {UUID(session.session_id) for session in found}

    async def _sandboxes(self, thread_ids: set[UUID]) -> dict[UUID, str]:
        """Each Thread's Sandbox; every app Thread's Session is registered with the gateway."""
        if not thread_ids:
            return {}
        found = {
            UUID(session.session_id): session.sandbox
            for session in await self._gateway.session_sandboxes({str(thread_id) for thread_id in thread_ids})
        }
        if missing := thread_ids - found.keys():
            raise SessionNotRegisteredError(min(missing))
        return found

    async def _view(self, thread: Thread) -> ThreadView:
        return _view(thread, await self._summaries.get(thread.id), (await self._sandboxes({thread.id}))[thread.id])

    async def archived_count(self) -> int:
        async with self._sessions() as session:
            return (await session.scalar(select(func.count()).select_from(Thread).where(Thread.archived))) or 0

    async def get_thread(self, thread_id: UUID) -> ThreadView | None:
        async with self._sessions() as session:
            thread = await session.get(Thread, thread_id)
        return None if thread is None else await self._view(thread)

    async def rename(self, thread_id: UUID, name: str | None) -> ThreadView:
        """Set or, with None, clear the thread's name."""
        async with self._sessions.begin() as session:
            thread = await _set_thread(session, thread_id, name=name)
            await notify(session, Channel.THREADS)
        return await self._view(thread)

    async def archive(self, thread_id: UUID) -> ThreadView:
        """Hide the thread from a default listing without touching its events."""
        return await self._set_archived(thread_id, True)

    async def unarchive(self, thread_id: UUID) -> ThreadView:
        return await self._set_archived(thread_id, False)

    async def _set_archived(self, thread_id: UUID, archived: bool) -> ThreadView:
        async with self._sessions.begin() as session:
            thread = await _set_thread(session, thread_id, archived=archived)
            await notify(session, Channel.THREADS)
        return await self._view(thread)


async def _set_thread(session: AsyncSession, thread_id: UUID, **values: object) -> Thread:
    """Write what an operator set on a Thread."""
    thread = await session.scalar(update(Thread).where(Thread.id == thread_id).values(**values).returning(Thread))
    if thread is None:
        raise ThreadNotFoundError(thread_id)
    return thread


def _view(thread: Thread, summary: FoldSummary, sandbox: str) -> ThreadView:
    last_turn_status = None if summary.last_turn_status is None else event_pb2.TurnStatus.Name(summary.last_turn_status)
    attachment = summary.feed.attached if summary.feed is not None else None
    end = summary.feed.end if summary.feed is not None else None
    harness_state = attachment.harness_state if attachment is not None else runner_pb2.HARNESS_STATE_UNSPECIFIED
    spec = attachment.spec if attachment is not None and attachment.HasField("spec") else None
    return ThreadView(
        id=thread.id,
        sandbox=sandbox,
        harness=None if spec is None else Harness(runner_pb2.Harness.Name(spec.harness)),
        model=None if spec is None else spec.model,
        cwd=None if spec is None else spec.cwd,
        created_at=thread.created_at,
        name=thread.name,
        archived=thread.archived,
        last_cursor=summary.folded_through,
        last_event_at=summary.last_event_at,
        last_model_activity_at=summary.last_model_activity_at,
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
