"""Durable GitHub refresh leases and generation-fenced observations."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentplane.notification_service.db import (
    GitHubInstallation,
    GitHubRepository,
    GitHubRepositoryAccess,
    GitHubSubject,
    GitHubSubjectRevision,
)
from agentplane.notification_service.models import SourceFailureKind

GITHUB_INGRESS_LOCK = 0x474854504E


@dataclass(frozen=True)
class AccessKey:
    app_id: int
    installation_id: int
    repository_id: int

    async def load(self, session: AsyncSession) -> GitHubRepositoryAccess:
        row = await session.get(GitHubRepositoryAccess, (self.app_id, self.installation_id, self.repository_id))
        assert row is not None
        return row


@dataclass(frozen=True)
class SubjectKey:
    repository_id: int
    kind: str
    subject_key: str

    async def load(self, session: AsyncSession) -> GitHubSubject:
        row = await session.get(GitHubSubject, (self.repository_id, self.kind, self.subject_key))
        assert row is not None
        return row


type RefreshKey = AccessKey | SubjectKey
type RefreshRow = GitHubRepositoryAccess | GitHubSubject


@dataclass(frozen=True)
class HeadRevision:
    repository_id: int
    repository_name: str
    sha: str


@dataclass(frozen=True)
class RefreshLease:
    key: RefreshKey
    claim: UUID
    generation: int
    installation_generation: int | None


@dataclass(frozen=True)
class AccessFence:
    key: AccessKey
    generation: int
    installation_generation: int


class RefreshDeferredError(Exception):
    """The shared row owns the diagnostic; only its scheduling deadline is propagated."""

    def __init__(self, until: datetime) -> None:
        self.until = until
        super().__init__("GitHub shared refresh deferred")


async def github_lock(session: AsyncSession) -> None:
    # Lock order: GitHub state/ingress, then inbox. Never held across remote I/O.
    await session.execute(select(func.pg_advisory_xact_lock(GITHUB_INGRESS_LOCK)))


async def installation_generation(session: AsyncSession, key: AccessKey) -> int:
    installation = await session.get(GitHubInstallation, (key.app_id, key.installation_id))
    assert installation is not None
    return installation.generation


async def access_valid(session: AsyncSession, row: GitHubRepositoryAccess, now: datetime) -> bool:
    return (
        row.error is None
        and row.valid_until is not None
        and row.valid_until > now
        and row.validated_installation_generation
        == await installation_generation(session, AccessKey(row.app_id, row.installation_id, row.repository_id))
    )


async def fences_valid(session: AsyncSession, fences: Sequence[AccessFence]) -> bool:
    now = datetime.now(UTC)
    for fence in fences:
        row = await fence.key.load(session)
        if (
            row.generation != fence.generation
            or row.validated_installation_generation != fence.installation_generation
            or not await access_valid(session, row, now)
        ):
            return False
    return True


async def add_revisions(session: AsyncSession, key: SubjectKey, revisions: Sequence[HeadRevision]) -> None:
    for revision in revisions:
        await session.execute(
            insert(GitHubRepository)
            .values(repository_id=revision.repository_id, full_name=revision.repository_name)
            .on_conflict_do_nothing()
        )
        await session.execute(
            insert(GitHubSubjectRevision)
            .values(
                repository_id=key.repository_id,
                kind=key.kind,
                subject_key=key.subject_key,
                head_repository_id=revision.repository_id,
                sha=revision.sha,
            )
            .on_conflict_do_nothing()
        )


class GitHubState:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], freshness_seconds: int) -> None:
        self.sessions = sessions
        self.freshness = timedelta(seconds=freshness_seconds)

    async def acquire(self, key: RefreshKey) -> RefreshLease | None:
        async with self.sessions.begin() as session:
            await github_lock(session)
            row = await key.load(session)
            now = datetime.now(UTC)
            if isinstance(row, GitHubRepositoryAccess):
                assert isinstance(key, AccessKey)
                generation = await installation_generation(session, key)
                fresh = await access_valid(session, row, now)
            else:
                generation = None
                fresh = (
                    row.error is None and row.last_success_at is not None and row.last_success_at + self.freshness > now
                )
            if fresh:
                return None
            if row.claim is not None and row.claim_until is not None and row.claim_until > now:
                raise RefreshDeferredError(row.claim_until)
            if row.next_attempt is not None and row.next_attempt > now:
                raise RefreshDeferredError(row.next_attempt)
            row.claim = uuid4()
            row.claim_until = now + timedelta(seconds=30)
            return RefreshLease(key, row.claim, row.generation, generation)

    async def leased(self, session: AsyncSession, lease: RefreshLease) -> RefreshRow:
        row = await lease.key.load(session)
        now = datetime.now(UTC)
        if (
            row.claim != lease.claim
            or row.claim_until is None
            or row.claim_until <= now
            or row.generation != lease.generation
            or (
                isinstance(lease.key, AccessKey)
                and await installation_generation(session, lease.key) != lease.installation_generation
            )
        ):
            raise RefreshDeferredError(now)
        return row

    async def succeed(
        self,
        lease: RefreshLease,
        *,
        fences: Sequence[AccessFence] = (),
        revisions: Sequence[HeadRevision] = (),
        repository_name: str | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            await github_lock(session)
            row = await self.leased(session, lease)
            if not await fences_valid(session, fences):
                raise RefreshDeferredError(datetime.now(UTC))
            if revisions:
                assert isinstance(lease.key, SubjectKey)
                await add_revisions(session, lease.key, revisions)
            now = datetime.now(UTC)
            row.last_success_at = now
            row.error_kind = row.error = row.error_since = row.error_observed_at = None
            row.claim = row.claim_until = row.next_attempt = None
            if isinstance(row, GitHubRepositoryAccess):
                if repository_name is not None:
                    repository = await session.get(GitHubRepository, row.repository_id)
                    assert repository is not None
                    repository.full_name = repository_name
                row.checked_at = now
                row.valid_until = now + self.freshness
                row.validated_installation_generation = lease.installation_generation

    async def fail(self, lease: RefreshLease, kind: SourceFailureKind, error: str, retry_seconds: int) -> datetime:
        async with self.sessions.begin() as session:
            await github_lock(session)
            row = await self.leased(session, lease)
            now = datetime.now(UTC)
            if row.error_kind != kind or row.error_since is None:
                row.error_since = now
            row.error_kind, row.error, row.error_observed_at = kind.value, error, now
            row.next_attempt = now + timedelta(seconds=retry_seconds)
            row.claim = row.claim_until = None
            if isinstance(row, GitHubRepositoryAccess):
                row.valid_until = None
                row.checked_at = now
            return row.next_attempt
