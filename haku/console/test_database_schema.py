"""Database-backed contracts for the session lifecycle derivation and constraints."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import Conversation, Session
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.session.status import (
    ENDED_SESSION_STATUSES,
    LEASED_SESSION_STATUSES,
    OPEN_SESSION_STATUSES,
    SessionStatus,
)

_NOW = datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC)


@dataclass(frozen=True)
class _Facts:
    """One reachable combination of the session row's lifecycle facts."""

    allocated: bool = False
    attached: bool = False
    close_requested: bool = False
    ended: bool = False
    error: str | None = None


_MATRIX = [
    pytest.param(_Facts(), SessionStatus.IDLE, id="created"),
    pytest.param(_Facts(allocated=True), SessionStatus.PROVISIONING, id="allocated"),
    pytest.param(_Facts(allocated=True, attached=True), SessionStatus.READY, id="runner-attached"),
    pytest.param(_Facts(close_requested=True), SessionStatus.CLOSING, id="idle-asked-to-close"),
    pytest.param(
        _Facts(allocated=True, attached=True, close_requested=True), SessionStatus.CLOSING, id="live-asked-to-close"
    ),
    pytest.param(_Facts(ended=True), SessionStatus.CLOSED, id="closed-before-allocation"),
    pytest.param(_Facts(allocated=True, attached=True, ended=True), SessionStatus.CLOSED, id="closed-after-serving"),
    pytest.param(_Facts(ended=True, error="never provisioned"), SessionStatus.FAILED, id="failed-unallocated"),
    pytest.param(
        _Facts(allocated=True, ended=True, error="sandbox never came up"),
        SessionStatus.FAILED,
        id="failed-while-provisioning",
    ),
    pytest.param(
        _Facts(allocated=True, attached=True, ended=True, error="lease expired"),
        SessionStatus.FAILED,
        id="failed-while-serving",
    ),
    pytest.param(
        _Facts(allocated=True, attached=True, close_requested=True, ended=True),
        SessionStatus.CLOSED,
        id="close-request-finished",
    ),
]


@pytest.fixture
async def operator_id(migrated_identity_store: PostgresOperatorIdentityStore) -> UUID:
    return await migrated_identity_store.resolve_configured_external_user_key("status-derivation-operator")


async def _insert_session(db: AsyncSession, operator_id: UUID, facts: _Facts) -> UUID:
    conversation_id, session_id = uuid4(), uuid4()
    db.add(
        Conversation(
            conversation_id=conversation_id,
            operator_id=operator_id,
            harness_kind=HarnessKind.CLAUDE_CODE,
            created_at=_NOW,
        )
    )
    await db.flush()
    db.add(
        Session(
            session_id=session_id,
            operator_id=operator_id,
            conversation_id=conversation_id,
            bridge_token_fingerprint=session_id.bytes if facts.allocated else None,
            bridge_connected_at=_NOW if facts.attached else None,
            lease_expires_at=_NOW + datetime.timedelta(minutes=1) if facts.allocated else None,
            close_requested_at=_NOW if facts.close_requested else None,
            ended_at=_NOW if facts.ended else None,
            error=facts.error,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    await db.flush()
    return session_id


@pytest.mark.parametrize(("facts", "expected"), _MATRIX)
async def test_the_row_facts_derive_the_same_member_in_python_and_sql(
    migrated_sessions: async_sessionmaker[AsyncSession], operator_id: UUID, facts: _Facts, expected: SessionStatus
) -> None:
    async with migrated_sessions.begin() as db:
        session_id = await _insert_session(db, operator_id, facts)

    async with migrated_sessions() as db:
        row = await db.get(Session, session_id)
        assert row is not None
        assert row.status is expected

        selected = await db.scalar(select(Session.status).where(Session.session_id == session_id))
        assert selected is expected

        named = (
            await db.execute(select(Session.session_id, Session.status).where(Session.session_id == session_id))
        ).one()
        assert named.status is expected

        found = await db.scalar(
            select(Session.session_id).where(Session.session_id == session_id, Session.status == expected)
        )
        assert found == session_id


@pytest.mark.parametrize(("facts", "expected"), _MATRIX)
async def test_the_set_filters_the_store_queries_by_agree_with_the_python_sets(
    migrated_sessions: async_sessionmaker[AsyncSession], operator_id: UUID, facts: _Facts, expected: SessionStatus
) -> None:
    async with migrated_sessions.begin() as db:
        session_id = await _insert_session(db, operator_id, facts)

    async with migrated_sessions() as db:
        for statuses in (OPEN_SESSION_STATUSES, ENDED_SESSION_STATUSES, LEASED_SESSION_STATUSES):
            matched = await db.scalar(
                select(Session.session_id).where(Session.session_id == session_id, Session.status.in_(statuses))
            )
            assert (matched == session_id) is (expected in statuses)


async def test_fact_shapes_the_vocabulary_cannot_say_are_unwritable(
    migrated_sessions: async_sessionmaker[AsyncSession], operator_id: UUID
) -> None:
    async def write_row(**columns: object) -> None:
        async with migrated_sessions.begin() as db:
            conversation_id = uuid4()
            db.add(
                Conversation(
                    conversation_id=conversation_id,
                    operator_id=operator_id,
                    harness_kind=HarnessKind.CLAUDE_CODE,
                    created_at=_NOW,
                )
            )
            await db.flush()
            db.add(
                Session(
                    session_id=uuid4(),
                    operator_id=operator_id,
                    conversation_id=conversation_id,
                    created_at=_NOW,
                    updated_at=_NOW,
                    **columns,
                )
            )
            await db.flush()

    async def rejected(constraint: str, **columns: object) -> None:
        with pytest.raises(IntegrityError, match=constraint):
            await write_row(**columns)

    await rejected("ck_sessions_error_ended", error="but still live")
    await rejected("ck_sessions_connected_allocated", bridge_connected_at=_NOW)
    await rejected("ck_sessions_allocation_lease", bridge_token_fingerprint=b"fp")
    await rejected("ck_sessions_allocation_lease", lease_expires_at=_NOW)
    await rejected("ck_sessions_claim_cleanup_ended", claim_cleaned_at=_NOW)


if __name__ == "__main__":
    pytest_bazel.main()
